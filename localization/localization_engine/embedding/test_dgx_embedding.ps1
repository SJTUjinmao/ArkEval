$ErrorActionPreference = "Stop"

$body = @{
  texts = @("hello from windows", "distributed embedding service")
  include_embeddings = $false
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8008/embed" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body | ConvertTo-Json -Depth 5
