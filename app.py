from flask import Flask, request, jsonify
from flask_cors import CORS, cross_origin
from azure.cosmos import CosmosClient, PartitionKey, exceptions
from auth0.management import Auth0
import requests
import os
import logging
from logging.config import dictConfig

# --- Logging Configuration ---
dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'stream': 'ext://flask.logging.wsgi_errors_stream',
            'formatter': 'default'
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'logging.log',
            'maxBytes': 1000000,
            'backupCount': 6,
            'formatter': 'default'
        }
    },
    'root': {
        'level': 'DEBUG',
        'handlers': ['console', 'file']
    }
})

app = Flask(__name__)
CORS(app)

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
    app.logger.debug("Connecting to Cosmos DB")
    client = CosmosClient(COSMOS_DB_URL, COSMOS_DB_KEY)
    database = client.create_database_if_not_exists(id=DATABASE_NAME)
    container = database.create_container_if_not_exists(
        id=CONTAINER_NAME,
        partition_key=PartitionKey(path="/id"),
        offer_throughput=400
    )
    app.logger.debug("Cosmos container ready")
    return container

def get_auth0_client():
    try:
        app.logger.info("Requesting Auth0 M2M token")
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
        app.logger.info("Auth0 M2M token acquired")
        return Auth0(AUTH0_DOMAIN, token_response.json()["access_token"])
    except Exception as e:
        app.logger.error(f"Failed to get Auth0 client: {e}", exc_info=True)
        raise

def ensure_list(param):
    if param is None:
        return []
    if isinstance(param, list):
        return param
    return [param]

@app.route('/createApp', methods=['POST'])
@cross_origin()
def create_auth0_app():
    app.logger.info("POST /createApp called")
    try:
        data = request.get_json()
        app.logger.debug(f"Request data: {data}")
        app_name = data.get('app')
        org_name = data.get('org_name')
        email = data.get('email')
        
        # Set critical defaults
        initiate_login_uri = data.get('initiate_login_uri', "http://localhost:3000")
        callback_urls = ensure_list(data.get('callback_urls', "http://localhost:3000/callback"))
        logout_urls = ensure_list(data.get('logout_urls', "http://localhost:3000/logout"))

        app.logger.info(f'Creating app "{app_name}" for org "{org_name}"')

        # Validate all required parameters
        if not all([app_name, org_name, email, initiate_login_uri]):
            missing = [k for k, v in {'app': app_name, 'org_name': org_name,
                                    'email': email, 'initiate_login_uri': initiate_login_uri}.items() if not v]
            app.logger.error(f'Missing required parameters: {missing}')
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        auth0 = get_auth0_client()

        # 1. Create Auth0 client with OIDC compliance and org enforcement
        auth0_app = auth0.clients.create({
            "name": app_name,
            "app_type": "spa",
            "callbacks": callback_urls,
            "allowed_logout_urls": logout_urls,
            "initiate_login_uri": initiate_login_uri,
            "organization_usage": "require",
            "organization_require_behavior": "pre_login_prompt",
            "oidc_conformant": True,
            "token_endpoint_auth_method": "none"
        })
        app.logger.info(f'Created OIDC-compliant client {auth0_app["client_id"]}')

        # 2. Create Organization
        org = auth0.organizations.create_organization({
            "name": org_name.lower().replace(" ", "-"),
            "display_name": org_name
        })
        app.logger.info(f'Created organization {org["id"]}')

        # 3. Enable connection for organization
        auth0.organizations.create_organization_connection(
            org["id"],
            {
                "connection_id": AUTH0_CONNECTION_ID,
                "assign_membership_on_login": True
            }
        )
        app.logger.info(f'Connected {AUTH0_CONNECTION_ID} to organization {org["id"]}')

        # 4. Send invitation
        invitation = auth0.organizations.create_organization_invitation(
            org["id"],
            {
                "inviter": {"name": "System Admin"},
                "invitee": {"email": email},
                "client_id": auth0_app["client_id"],
                "send_invitation_email": True
            }
        )
        app.logger.info(f'Sent invitation to {email}')

        # --- Add Okta domain and callback URLs to response ---
        okta_domain = AUTH0_DOMAIN  # <-- Replace with your actual Okta domain

        return jsonify({
            "client_id": auth0_app["client_id"],
            "org_id": org["id"],
            "initiate_login_uri": initiate_login_uri,
            "oidc_conformant": True,
            "okta_domain": okta_domain,
            "callback_urls": callback_urls
        }), 201

    except Exception as e:
        app.logger.error(f'Critical error in /createApp: {str(e)}', exc_info=True)
        return jsonify({
            "error": "Application creation failed",
            "details": str(e)
        }), 500


@app.route('/write', methods=['POST'])
@cross_origin()
def write_or_update_data():
    app.logger.info("POST /write called")
    try:
        data = request.get_json()
        app.logger.debug(f"Write data: {data}")
        if not data or 'id' not in data:
            app.logger.warning("Invalid data for write: missing 'id'")
            return jsonify({"error": "Invalid data. 'id' is required."}), 400

        container = get_cosmos_container()
        container.upsert_item(body=data)
        app.logger.info(f"Data written/updated for id: {data['id']}")
        return jsonify({"message": "Data written or updated successfully"}), 201
    except exceptions.CosmosHttpResponseError as e:
        app.logger.error(f"Cosmos DB error: {e}", exc_info=True)
        return jsonify({"error": "Database error"}), 500

@app.route('/retrieve/<tenant_id>', methods=['GET'])
@cross_origin()
def retrieve_data(tenant_id):
    try:
        container = get_cosmos_container()
        query = "SELECT * FROM c WHERE c.TenantId = @tenant_id"
        parameters = [{"name": "@tenant_id", "value": tenant_id}]

        items = list(container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ))

        if not items:
            return jsonify({"error": "Item not found"}), 404

        return jsonify(items[0]), 200

    except exceptions.CosmosHttpResponseError as e:
        return jsonify({"error": "Database error", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500

@app.route('/retrieve-all', methods=['GET'])
@cross_origin()
def retrieve_all():
    app.logger.info("GET /retrieve-all called")
    try:
        container = get_cosmos_container()
        query = "SELECT * FROM c"
        items = list(container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        app.logger.info(f"Retrieved all items. Count: {len(items)}")
        return jsonify(items), 200
    except exceptions.CosmosHttpResponseError as e:
        app.logger.error(f"Cosmos DB error: {e}", exc_info=True)
        return jsonify({"error": "Database error"}), 500

@app.route('/delete/<id>', methods=['DELETE'])
@cross_origin()
def delete_data(id):
    app.logger.info(f"DELETE /delete/{id} called")
    try:
        container = get_cosmos_container()
        container.delete_item(item=id, partition_key=id)
        app.logger.info(f"Item deleted: {id}")
        return jsonify({"message": "Data deleted successfully"}), 200
    except exceptions.CosmosResourceNotFoundError:
        app.logger.warning(f"Item not found for delete: {id}")
        return jsonify({"error": "Item not found"}), 404
    except exceptions.CosmosHttpResponseError as e:
        app.logger.error(f"Cosmos DB error: {e}", exc_info=True)
        return jsonify({"error": "Database error"}), 500

@app.route('/edit/<id>', methods=['PUT'])
@cross_origin()
def edit_data(id):
    app.logger.info(f"PUT /edit/{id} called")
    try:
        updated_data = request.get_json()
        app.logger.debug(f"Edit data for {id}: {updated_data}")
        if not updated_data:
            app.logger.warning("No JSON body provided for edit")
            return jsonify({"error": "Invalid input. JSON body is required."}), 400

        container = get_cosmos_container()
        item = container.read_item(item=id, partition_key=id)
        item.update(updated_data)
        container.replace_item(item=item, body=item)
        app.logger.info(f"Item updated: {id}")
        return jsonify({"message": "Data updated successfully"}), 200
    except exceptions.CosmosResourceNotFoundError:
        app.logger.warning(f"Item not found for edit: {id}")
        return jsonify({"error": "Item not found"}), 404
    except exceptions.CosmosHttpResponseError as e:
        app.logger.error(f"Cosmos DB error: {e}", exc_info=True)
        return jsonify({"error": "Database error"}), 500

@app.route('/trigger-deploy', methods=['POST'])
@cross_origin()
def trigger_deployment():
    app.logger.info("POST /trigger-deploy called")
    try:
        data = request.get_json()
        app.logger.debug(f"Trigger deploy payload: {data}")

        repo = data.get('repo')
        workflow_id = data.get('workflow_id')
        inputs = data.get('inputs', {})
        
        app.logger.info(f'GITHUB_OWNER: {GITHUB_OWNER}')
        app.logger.info(f'GITHUB_PAT: {"***" if GITHUB_PAT else "Not set"}')

        if not repo:
            app.logger.warning('Missing required parameter: repo')
            return jsonify({"error": "Missing required parameter: repo"}), 400
        if not workflow_id:
            app.logger.warning('Missing required parameter: workflow_id')
            return jsonify({"error": "Missing required parameter: workflow_id"}), 400
        if not GITHUB_PAT or not GITHUB_OWNER:
            app.logger.error('Server misconfiguration detected - missing GITHUB_PAT or GITHUB_OWNER')
            return jsonify({"error": "Server misconfiguration"}), 500

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

        app.logger.info(f'Dispatching workflow to GitHub: {url}')
        app.logger.debug(f'Request payload: {payload}')

        response = requests.post(url, json=payload, headers=headers, timeout=30)
        app.logger.info(f'GitHub API response status: {response.status_code}')

        if response.status_code == 204:
            app.logger.info('Successfully triggered workflow')
            return jsonify({
                "status": "Workflow triggered successfully",
                "repo": repo,
                "workflow_id": workflow_id,
                "inputs": inputs
            }), 200

        app.logger.error(f'Workflow trigger failed. GitHub response: {response.text}')
        return jsonify({
            "error": "Failed to trigger workflow",
            "repo": repo,
            "workflow_id": workflow_id,
            "details": response.json().get('message', 'Unknown error')
        }), response.status_code

    except requests.exceptions.RequestException as e:
        app.logger.error(f'Network error occurred: {str(e)}', exc_info=True)
        return jsonify({"error": "Connection to GitHub failed"}), 500
    except Exception as e:
        app.logger.error(f'Unexpected error: {str(e)}', exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    app.logger.info("Starting Flask app")
    app.run(host='0.0.0.0', port=5000, debug=False)
