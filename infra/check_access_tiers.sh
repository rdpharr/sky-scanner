#!/usr/bin/env bash
# Query BigQuery as each public-facing service account and check that it can
# read the public views and nothing else. Needs token-creator on the accounts
# (granted to the owner in infra/terraform/iam.tf). Exit code 1 on any surprise.
set -uo pipefail
PROJECT="${PROJECT:-sky-scanner-pharr}"
status=0

check() {  # token, label, expected (allow|deny), sql
    local result
    result=$(curl -s -X POST -H "Authorization: Bearer $1" -H "Content-Type: application/json" \
        "https://bigquery.googleapis.com/bigquery/v2/projects/$PROJECT/queries" \
        -d "{\"query\": \"$4\", \"useLegacySql\": false}" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); print("deny" if "error" in d else "allow")')
    if [ "$result" = "$3" ]; then echo "  ok    $3  $2"; else echo "  FAIL  got $result, expected $3: $2"; status=1; fi
}

for sa in sky-web sky-mcp sky-notable; do
    token=$(gcloud auth print-access-token --impersonate-service-account="$sa@$PROJECT.iam.gserviceaccount.com") || exit 2
    echo "$sa"
    check "$token" "public.pub_aircraft" allow "select count(*) from public.pub_aircraft"
    check "$token" "public.pub_live_snapshots (view over raw)" allow "select count(*) from public.pub_live_snapshots"
    check "$token" "analytics.dim_aircraft (owner names, LADD)" deny "select count(*) from analytics.dim_aircraft"
    check "$token" "raw.aircraft_metadata" deny "select count(*) from raw.aircraft_metadata"
done
exit $status
