targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd environment name; drives resource naming')
param environmentName string

@minLength(1)
@description('Primary region (EU residency: swedencentral)')
param location string

@secure()
param tavilyApiKey string = ''

@secure()
param apiKey string = ''

var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    tags: tags
    tavilyApiKey: tavilyApiKey
    apiKey: apiKey
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.acrEndpoint
output SERVICE_AGENT_URI string = resources.outputs.appUri
output AZURE_OPENAI_ENDPOINT string = resources.outputs.openaiEndpoint
