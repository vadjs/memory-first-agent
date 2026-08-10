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

@description('Deploying principal (CI service principal or local azd user); granted Key Vault read for agent secrets')
param principalId string = ''

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
    deployPrincipalId: principalId
  }
}

output AZURE_OPENAI_ENDPOINT string = resources.outputs.openaiEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.foundryProjectEndpoint
output AZURE_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString
