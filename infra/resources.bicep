param location string
param tags object

@secure()
param tavilyApiKey string

@secure()
param apiKey string

var token = toLower(uniqueString(subscription().id, resourceGroup().id))

// ---------------------------------------------------------------- identity ---
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-agent-${token}'
  location: location
  tags: tags
}

// ------------------------------------------------------------ observability ---
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${token}'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ------------------------------------------------------------------ models ---
resource foundry 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: 'ai-${token}'
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  tags: tags
  properties: {
    customSubDomainName: 'ai-${token}'
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true // Foundry projects (portal presence + hosted agents)
  }
}

resource luna 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: 'gpt-5.6-luna'
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.6-luna', version: '2026-07-09' }
  }
}

resource nano 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: 'gpt-5-nano'
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5-nano', version: '2025-08-07' }
  }
  dependsOn: [luna]
}

resource embed 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = {
  parent: foundry
  name: 'text-embedding-3-small'
  sku: { name: 'GlobalStandard', capacity: 60 }
  properties: {
    model: { format: 'OpenAI', name: 'text-embedding-3-small', version: '1' }
  }
  dependsOn: [nano]
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = {
  parent: foundry
  name: 'memory-first'
  location: location
  identity: { type: 'SystemAssigned' }
  tags: tags
  properties: {
    displayName: 'memory-first-agent'
    description: 'Memory-first web agent: model deployments and the Foundry-hosted agent variant'
  }
}

// ------------------------------------------------------------------ memory ---
resource redis 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: 'redis-${token}'
  location: location
  tags: tags
  sku: { name: 'Balanced_B0' }
  properties: {
    publicNetworkAccess: 'Enabled' // reference environment; private endpoint in a locked-down estate
  }
}

resource redisDb 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    evictionPolicy: 'NoEviction' // required by RediSearch
    clusteringPolicy: 'EnterpriseCluster'
    accessKeysAuthentication: 'Enabled' // reference env; production path is Entra ID auth (blueprint)
    modules: [
      { name: 'RediSearch' }
    ]
  }
}

var redisUrl = 'rediss://default:${redisDb.listKeys().primaryKey}@${redis.properties.hostName}:10000'

// ----------------------------------------------------------------- secrets ---
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv${token}'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

resource secretRedis 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'redis-url'
  properties: { value: redisUrl }
}

resource secretTavily 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'tavily-api-key'
  properties: { value: tavilyApiKey }
}

resource secretApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'api-key'
  properties: { value: apiKey }
}

// ---------------------------------------------------------------- registry ---
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acr${token}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ------------------------------------------------------------------- RBAC ----
var roleAcrPull = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var roleKvSecretsUser = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)
var roleOpenAIUser = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identity.id, roleAcrPull)
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: roleAcrPull
    principalType: 'ServicePrincipal'
  }
}

resource kvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, roleKvSecretsUser)
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: roleKvSecretsUser
    principalType: 'ServicePrincipal'
  }
}

resource openaiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, identity.id, roleOpenAIUser)
  properties: {
    principalId: identity.properties.principalId
    roleDefinitionId: roleOpenAIUser
    principalType: 'ServicePrincipal'
  }
}

// ------------------------------------------------------------- compute ------
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${token}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-agent-${token}'
  location: location
  tags: union(tags, { 'azd-service-name': 'agent' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: [
        { name: 'redis-url', keyVaultUrl: secretRedis.properties.secretUri, identity: identity.id }
        {
          name: 'tavily-api-key'
          keyVaultUrl: secretTavily.properties.secretUri
          identity: identity.id
        }
        { name: 'api-key', keyVaultUrl: secretApiKey.properties.secretUri, identity: identity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest' // azd replaces on deploy
          env: [
            { name: 'AZURE_OPENAI_ENDPOINT', value: foundry.properties.endpoint }
            { name: 'USE_MANAGED_IDENTITY', value: 'true' }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'CHAT_DEPLOYMENT', value: 'gpt-5.6-luna' }
            { name: 'UTILITY_DEPLOYMENT', value: 'gpt-5-nano' }
            { name: 'EMBED_DEPLOYMENT', value: 'text-embedding-3-small' }
            { name: 'REDIS_URL', secretRef: 'redis-url' }
            { name: 'TAVILY_API_KEY', secretRef: 'tavily-api-key' }
            { name: 'API_KEY', secretRef: 'api-key' }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsights.properties.ConnectionString
            }
          ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 2 }
    }
  }
  dependsOn: [acrPull, kvSecrets, openaiUser, embed]
}

output acrEndpoint string = acr.properties.loginServer
output appUri string = 'https://${app.properties.configuration.ingress.fqdn}'
output openaiEndpoint string = foundry.properties.endpoint
output foundryProjectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}'
