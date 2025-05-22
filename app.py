from flask import Flask, request, jsonify
from flask_cors import CORS, cross_origin
from azure.cosmos import CosmosClient, PartitionKey, exceptions
from auth0.management import Auth0
import requests
import os
import logging

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)

# Environment variables (dummy defaults for local dev)
COSMOS_DB_URL = os.getenv("COSMOS_DB_URL", "https://dummy.documents.azure.com:443/")
COSMOS_DB_KEY = os.getenv("COSMOS_DB_KEY", "dummy-key") 
DATABASE_NAME = os.getenv("DATABASE_NAME", "testdb")
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "testcontainer")

AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "dev-abc123.auth0.com")
AUTH0_M2M_CLIENT_ID = os.getenv("AUTH0_M2M_CLIENT_ID", "dummy_client_id")
AUTH0_M2M_CLIENT_SECRET = os.getenv("AUTH0_M2M_CLIENT_SECRET", "dummy_client_secret")
AUTH0_CONNECTION_ID = os.getenv("AUTH0_CONNECTION_ID", "con_123456")

GITHUB_PAT = os.getenv("GITHUB_PAT", "ghp_dummyPAT")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "dummy-owner")

def get_cosmos_container():
    logger.debug("Initializing Cosmos DB client and container.")
    client = CosmosClient(COSMOS_DB_URL, COSMOS_DB_KEY)
    database = client.create_database_if_not_exists(id=DATABASE_NAME)
    container = database.create_container_if_not_exists(
        id=CONTAINER_NAME,
        partition_key=PartitionKey(path="/id"),
        offer_throughput=400
    )
    return container

def get_auth0_client():
    try:
        logger.info("Requesting Auth0 management token.")
        token_response = requests.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            json={
                "client_id": AUTH0_M2M_CLIENT_ID,
                "client_secret": AUTH0_M2M_CLIENT_SECRET,
                "audience": f"https://{AUTH0_DOMAIN}/api/v2/",
                "grant_type": "client_credentials"
            },
            timeout=10
        )
        token_response.raise_for_status()
        logger.info("Auth0 management token received successfully.")
        return Auth0(AUTH0_DOMAIN, token_response.json()["access_token"])
    except Exception as e:
        logger.error(f"Failed to get Auth0 client: {type(e).__name__}")
        raise

@app.route('/createApp', methods=['POST'])
@cross_origin()
def create_auth0_app():
    try:
        data = request.get_json()
        app_name = data.get('app')
        org_name = data.get('org_name')
        email = data.get('email')

        logger.info(f"Received createApp request for app='{app_name}', org='{org_name}', email='{email}'.")

        if not all([app_name, org_name, email]):
            logger.warning("Missing required parameters in /createApp.")
            return jsonify({"error": "Missing required parameters"}), 400

        auth0 = get_auth0_client()

        auth0_app = auth0.clients.create({
            "name": app_name,
            "app_type": "spa",
            "callbacks": ["http://localhost:3000/callback"],
            "organization_usage": "require"
        })

        org = auth0.organizations.create_organization({
            "name": org_name.lower().replace(" ", "-"),
            "display_name": org_name
        })

        auth0.organizations.create_organization_connection(
            org["id"],
            {
                "connection_id": AUTH0_CONNECTION_ID,
                "assign_membership_on_login": True
            }
        )

        invitation = auth0.organizations.create_organization_invitation(
            org["id"],
            {
                "inviter": {"name": "System Admin"},
                "invitee": {"email": email},
                "client_id": auth0_app["client_id"],
                "send_invitation_email": True
            }
        )

        logger.info(f"Auth0 app and org created successfully for '{app_name}'.")
        return jsonify({
            "client_id": auth0_app["client_id"],
            "org_id": org["id"],
            "invitation_url": invitation["ticket_url"]
        }), 201

    except Exception as e:
        logger.error(f"Error in /createApp: {type(e).__name__}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/write', methods=['POST'])
@cross_origin()
def write_or_update_data():
    try:
        data = request.get_json()
        if not data or 'id' not in data:
            logger.warning("Invalid data in /write: missing 'id'.")
            return jsonify({"error": "Invalid data. 'id' is required."}), 400

        container = get_cosmos_container()
        container.upsert_item(body=data)
        logger.info(f"Data written/updated for id={data['id']}.")
        return jsonify({"message": "Data written or updated successfully"}), 201
    except exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB error in /write: {type(e).__name__}")
        return jsonify({"error": "Database error"}), 500

@app.route('/retrieve/<id>', methods=['GET'])
@cross_origin()
def retrieve_data(id):
    try:
        container = get_cosmos_container()
        item = container.read_item(item=id, partition_key=id)
        logger.info(f"Data retrieved for id={id}.")
        return jsonify(item), 200
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"Item not found in /retrieve for id={id}.")
        return jsonify({"error": "Item not found"}), 404
    except exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB error in /retrieve: {type(e).__name__}")
        return jsonify({"error": "Database error"}), 500

@app.route('/retrieve-all', methods=['GET'])
@cross_origin()
def retrieve_all():
    try:
        container = get_cosmos_container()
        query = "SELECT * FROM c"
        items = list(container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        logger.info("All data retrieved from Cosmos DB.")
        return jsonify(items), 200
    except exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB error in /retrieve-all: {type(e).__name__}")
        return jsonify({"error": "Database error"}), 500

@app.route('/delete/<id>', methods=['DELETE'])
@cross_origin()
def delete_data(id):
    try:
        container = get_cosmos_container()
        container.delete_item(item=id, partition_key=id)
        logger.info(f"Data deleted for id={id}.")
        return jsonify({"message": "Data deleted successfully"}), 200
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"Item not found in /delete for id={id}.")
        return jsonify({"error": "Item not found"}), 404
    except exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB error in /delete: {type(e).__name__}")
        return jsonify({"error": "Database error"}), 500

@app.route('/edit/<id>', methods=['PUT'])
@cross_origin()
def edit_data(id):
    try:
        updated_data = request.get_json()
        if not updated_data:
            logger.warning("Invalid input in /edit: JSON body is required.")
            return jsonify({"error": "Invalid input. JSON body is required."}), 400

        container = get_cosmos_container()
        item = container.read_item(item=id, partition_key=id)
        item.update(updated_data)
        container.replace_item(item=item, body=item)
        logger.info(f"Data updated for id={id}.")
        return jsonify({"message": "Data updated successfully"}), 200
    except exceptions.CosmosResourceNotFoundError:
        logger.warning(f"Item not found in /edit for id={id}.")
        return jsonify({"error": "Item not found"}), 404
    except exceptions.CosmosHttpResponseError as e:
        logger.error(f"Cosmos DB error in /edit: {type(e).__name__}")
        return jsonify({"error": "Database error"}), 500

@app.route('/trigger-deploy', methods=['POST'])
@cross_origin()
def trigger_deployment():
    try:
        data = request.get_json()

        repo = data.get('repo')
        workflow_id = data.get('workflow_id')
        inputs = data.get('inputs', {})

        if not repo:
            logger.warning("Missing 'repo' parameter in /trigger-deploy.")
            return jsonify({"error": "Missing required parameter: repo"}), 400
        if not workflow_id:
            logger.warning("Missing 'workflow_id' parameter in /trigger-deploy.")
            return jsonify({"error": "Missing required parameter: workflow_id"}), 400
        if not GITHUB_PAT or not GITHUB_OWNER:
            logger.error("Server misconfiguration: missing GitHub credentials.")
            return jsonify({"error": "Server misconfiguration"}), 500

        logger.info(f"Triggering workflow '{workflow_id}' on repo '{repo}' with inputs: {list(inputs.keys())}")
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{repo}/actions/workflows/{workflow_id}/dispatches"
        headers = {
            "Authorization": f"Bearer {GITHUB_PAT}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        payload = {
            "ref": "main",
            "inputs": inputs
        }

        response = requests.post(url, json=payload, headers=headers, timeout=30)

        if response.status_code == 204:
            logger.info(f"Workflow '{workflow_id}' triggered successfully for repo '{repo}'.")
            return jsonify({
                "status": "Workflow triggered successfully",
                "repo": repo,
                "workflow_id": workflow_id,
                "inputs": inputs
            }), 200

        logger.error(f"Failed to trigger workflow '{workflow_id}' for repo '{repo}'. Status code: {response.status_code}")
        return jsonify({
            "error": "Failed to trigger workflow",
            "repo": repo,
            "workflow_id": workflow_id,
            "details": "Workflow dispatch failed"
        }), response.status_code

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error in /trigger-deploy: {type(e).__name__}")
        return jsonify({"error": "Connection to GitHub failed"}), 500
    except Exception as e:
        logger.error(f"Internal server error in /trigger-deploy: {type(e).__name__}")
        return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    logger.info("Starting Flask app on port 5000.")
    app.run(host='0.0.0.0', port=5000, debug=False)
