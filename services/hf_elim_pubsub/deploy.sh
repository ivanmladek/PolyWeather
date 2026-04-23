#!/usr/bin/env bash
#
# Deploy HF Elimination Arbitrage service to GCP using Terraform.
#
# Two phases:
#   1. Build & push container image (gcloud builds submit)
#   2. Provision all infra via Terraform (Cloud Run, Pub/Sub, Scheduler, IAM)
#
# Prerequisites:
#   - gcloud CLI authenticated (gcloud auth application-default login)
#   - terraform >= 1.5 installed
#   - Secrets exist in Secret Manager: TELEGRAM_BOT_TOKEN, POSTPEAK_ELIM_CHAT_ID
#
# Usage:
#   cd /path/to/PolyWeather   (project root — Docker build context)
#   bash services/hf_elim_pubsub/deploy.sh
#
set -euo pipefail

PROJECT=${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}
REGION=${GCP_REGION:-us-central1}
SERVICE=hf-elim-pubsub
IMAGE="gcr.io/${PROJECT}/${SERVICE}"
TF_DIR="services/hf_elim_pubsub/terraform"

echo "=== HF Elimination Arbitrage — Terraform Deploy ==="
echo "  Project:  ${PROJECT}"
echo "  Region:   ${REGION}"
echo "  Image:    ${IMAGE}"
echo ""

# ---------------------------------------------------------------------------
# Phase 1: Build container image via Cloud Build
# ---------------------------------------------------------------------------
echo ">>> Building container image ..."
gcloud builds submit \
    --project="${PROJECT}" \
    --tag="${IMAGE}" \
    --dockerfile=services/hf_elim_pubsub/Dockerfile \
    .

# ---------------------------------------------------------------------------
# Phase 2: Terraform — provision all GCP infra
# ---------------------------------------------------------------------------
echo ""
echo ">>> Initializing Terraform ..."
terraform -chdir="${TF_DIR}" init

echo ">>> Planning ..."
terraform -chdir="${TF_DIR}" plan \
    -var="project_id=${PROJECT}" \
    -var="region=${REGION}" \
    -var="image=${IMAGE}"

echo ""
read -rp "Apply this plan? [y/N] " confirm
if [[ "${confirm}" =~ ^[Yy]$ ]]; then
    echo ">>> Applying ..."
    terraform -chdir="${TF_DIR}" apply -auto-approve \
        -var="project_id=${PROJECT}" \
        -var="region=${REGION}" \
        -var="image=${IMAGE}"

    echo ""
    echo "=== Deployment complete ==="
    terraform -chdir="${TF_DIR}" output
    echo ""
    SERVICE_URL=$(terraform -chdir="${TF_DIR}" output -raw service_url)
    echo "  Test publisher:"
    echo "    curl -X POST ${SERVICE_URL}/publish -H 'Authorization: Bearer \$(gcloud auth print-identity-token)'"
    echo ""
    echo "  View logs:"
    echo "    gcloud run services logs read ${SERVICE} --region=${REGION} --project=${PROJECT}"
else
    echo "Aborted."
fi
