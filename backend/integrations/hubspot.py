# hubspot.py

import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
import requests
from integrations.integration_item import IntegrationItem

from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

CLIENT_ID = '7079c979-623c-4ec2-a26f-b51b8d62e4d2'
CLIENT_SECRET = '7cc64f1b-6301-4461-9a88-a32d9c135b5f'
REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
SCOPES = 'crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read'
authorization_url = f'https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPES}'


async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)

    return f'{authorization_url}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(encoded_state)

    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')

    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')

    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                'https://api.hubapi.com/oauth/v1/token',
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': REDIRECT_URI,
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                },
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                }
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )

    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=600)
    
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)


async def get_hubspot_credentials(user_id, org_id):
     
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    credentials = json.loads(credentials)
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials


def create_integration_item_metadata_object(response_json, item_type: str) -> IntegrationItem:
    """Create an IntegrationItem from HubSpot API response"""
    properties = response_json.get('properties', {})
    
    # Determine name based on item type
    if item_type == 'Contact':
        firstname = properties.get('firstname', '')
        lastname = properties.get('lastname', '')
        name = f"{firstname} {lastname}".strip() or properties.get('email', 'Unknown Contact')
    elif item_type == 'Company':
        name = properties.get('name', 'Unknown Company')
    elif item_type == 'Deal':
        name = properties.get('dealname', 'Unknown Deal')
    else:
        name = f'{item_type} {response_json.get("id", "")}'

    integration_item_metadata = IntegrationItem(
        id=f"{response_json.get('id')}_{item_type}",
        name=name,
        type=item_type,
        creation_time=properties.get('createdate'),
        last_modified_time=properties.get('hs_lastmodifieddate'),
    )

    return integration_item_metadata


async def get_items_hubspot(credentials) -> list[IntegrationItem]:
    """Fetch contacts, companies, and deals from HubSpot"""
    credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    headers = {'Authorization': f'Bearer {access_token}'}
    
    list_of_integration_item_metadata = []

    # Fetch Contacts
    contacts_response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/contacts',
        headers=headers,
        params={'limit': 100, 'properties': 'firstname,lastname,email,createdate,hs_lastmodifieddate'}
    )
    if contacts_response.status_code == 200:
        contacts = contacts_response.json().get('results', [])
        for contact in contacts:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(contact, 'Contact')
            )

    # Fetch Companies
    companies_response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/companies',
        headers=headers,
        params={'limit': 100, 'properties': 'name,createdate,hs_lastmodifieddate'}
    )
    if companies_response.status_code == 200:
        companies = companies_response.json().get('results', [])
        for company in companies:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(company, 'Company')
            )

    # Fetch Deals
    deals_response = requests.get(
        'https://api.hubapi.com/crm/v3/objects/deals',
        headers=headers,
        params={'limit': 100, 'properties': 'dealname,createdate,hs_lastmodifieddate'}
    )
    if deals_response.status_code == 200:
        deals = deals_response.json().get('results', [])
        for deal in deals:
            list_of_integration_item_metadata.append(
                create_integration_item_metadata_object(deal, 'Deal')
            )

    return list_of_integration_item_metadata