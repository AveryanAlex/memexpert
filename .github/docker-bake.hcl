variable "CACHE_SCOPE_SUFFIX" {
  default = "main"
}

group "ci" {
  targets = ["main", "worker", "frontend", "e2e-runner"]
}

target "main" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "main"
  tags       = ["memexpert-main:ci"]
  cache-from = CACHE_SCOPE_SUFFIX == "main" ? [
    "type=gha,scope=memexpert-main-main",
  ] : [
    "type=gha,scope=memexpert-main-${CACHE_SCOPE_SUFFIX}",
    "type=gha,scope=memexpert-main-main",
  ]
  cache-to = ["type=gha,scope=memexpert-main-${CACHE_SCOPE_SUFFIX},mode=max"]
}

target "worker" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "worker"
  tags       = ["memexpert-worker:ci"]
  cache-from = CACHE_SCOPE_SUFFIX == "main" ? [
    "type=gha,scope=memexpert-worker-main",
  ] : [
    "type=gha,scope=memexpert-worker-${CACHE_SCOPE_SUFFIX}",
    "type=gha,scope=memexpert-worker-main",
  ]
  cache-to = ["type=gha,scope=memexpert-worker-${CACHE_SCOPE_SUFFIX},mode=max"]
}

target "frontend" {
  context    = "."
  dockerfile = "frontend/Dockerfile"
  tags       = ["memexpert-frontend:ci"]
  cache-from = CACHE_SCOPE_SUFFIX == "main" ? [
    "type=gha,scope=memexpert-frontend-main",
  ] : [
    "type=gha,scope=memexpert-frontend-${CACHE_SCOPE_SUFFIX}",
    "type=gha,scope=memexpert-frontend-main",
  ]
  cache-to = ["type=gha,scope=memexpert-frontend-${CACHE_SCOPE_SUFFIX},mode=max"]
}

target "e2e-runner" {
  context    = "."
  dockerfile = "e2e/Dockerfile"
  tags       = ["memexpert-e2e-runner:ci"]
  cache-from = CACHE_SCOPE_SUFFIX == "main" ? [
    "type=gha,scope=memexpert-e2e-runner-main",
  ] : [
    "type=gha,scope=memexpert-e2e-runner-${CACHE_SCOPE_SUFFIX}",
    "type=gha,scope=memexpert-e2e-runner-main",
  ]
  cache-to = ["type=gha,scope=memexpert-e2e-runner-${CACHE_SCOPE_SUFFIX},mode=max"]
}
