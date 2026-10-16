#!/usr/bin/env bash
# One-time bootstrap of the Google Cloud project, before Terraform takes over.
# Creates the project, links billing, enables APIs, sets a budget alert and
# creates the Terraform state bucket. Safe to re-run.
#
# Usage: BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX infra/bootstrap.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sky-scanner-pharr}"
REGION="${REGION:-us-central1}"
BUDGET_USD="${BUDGET_USD:-25}"
: "${BILLING_ACCOUNT:?Set BILLING_ACCOUNT (gcloud billing accounts list)}"

if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud projects create "$PROJECT_ID" --name="Sky Scanner"
fi
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"

gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"
gcloud auth application-default set-quota-project "$PROJECT_ID"

gcloud services enable --project="$PROJECT_ID" \
    artifactregistry.googleapis.com \
    bigquery.googleapis.com \
    bigquerystorage.googleapis.com \
    billingbudgets.googleapis.com \
    cloudquotas.googleapis.com \
    cloudresourcemanager.googleapis.com \
    cloudscheduler.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    logging.googleapis.com \
    monitoring.googleapis.com \
    run.googleapis.com \
    secretmanager.googleapis.com \
    serviceusage.googleapis.com \
    storage.googleapis.com \
    sts.googleapis.com

# Budget emails go to the billing account's admins at 50/90/100% of spend.
if ! gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
        --format='value(displayName)' | grep -qx "sky-scanner"; then
    gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
        --display-name="sky-scanner" \
        --budget-amount="${BUDGET_USD}USD" \
        --filter-projects="projects/$PROJECT_ID" \
        --threshold-rule=percent=0.5 \
        --threshold-rule=percent=0.9 \
        --threshold-rule=percent=1.0
fi

STATE_BUCKET="gs://${PROJECT_ID}-tfstate"
if ! gcloud storage buckets describe "$STATE_BUCKET" >/dev/null 2>&1; then
    gcloud storage buckets create "$STATE_BUCKET" --location="$REGION" \
        --uniform-bucket-level-access --public-access-prevention
    gcloud storage buckets update "$STATE_BUCKET" --versioning
fi

echo "Bootstrapped $PROJECT_ID (region $REGION, budget \$$BUDGET_USD, state $STATE_BUCKET)"
