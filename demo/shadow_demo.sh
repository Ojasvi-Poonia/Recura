#!/usr/bin/env bash
# Shadow mode in one screen. In another tab first:
#
#   RAZORPAY_WEBHOOK_SECRET=whsec_demo RECURA_API_KEY=demo_key make serve SHADOW=1
#
# Sends three failures and then the payment that settles one of them. Recura decides on
# each failure and logs it; nothing is retried and nothing is sent.
set -euo pipefail
HOST=${HOST:-localhost:8000}
SECRET=${RAZORPAY_WEBHOOK_SECRET:-whsec_demo}
API_KEY=${RECURA_API_KEY:-demo_key}

# Recent timestamps, because the contract is evaluated at the real time of arrival: a
# failure older than episode.max_days is correctly refused as a closed episode.
NOW=$(date +%s)
T1=$((NOW - 14400)); T2=$((NOW - 13800)); T3=$((NOW - 13200)); T4=$((NOW - 3600))

send() {  # send <event-id> <json-body>
  local sig
  sig=$(python3 -c "import hmac,hashlib,sys;print(hmac.new(sys.argv[1].encode(),sys.argv[2].encode(),hashlib.sha256).hexdigest())" "$SECRET" "$2")
  curl -s -X POST "$HOST/webhooks/razorpay" -H "x-razorpay-signature: $sig" \
       -H "x-razorpay-event-id: $1" -d "$2"; echo
}

echo "1) UPI payment fails for lack of funds (order_1):"
send "evt_shadow_1_$NOW" "{\"event\":\"payment.failed\",\"created_at\":$T1,\"payload\":{\"payment\":{\"entity\":{\"id\":\"pay_shadow_1_$NOW\",\"order_id\":\"order_1_$NOW\",\"amount\":249900,\"currency\":\"INR\",\"method\":\"upi\",\"created_at\":$T1,\"error_reason\":\"insufficient_funds\",\"error_source\":\"customer\"}}}}"

echo "2) card payment fails a risk check (order_2):"
send "evt_shadow_2_$NOW" "{\"event\":\"payment.failed\",\"created_at\":$T2,\"payload\":{\"payment\":{\"entity\":{\"id\":\"pay_shadow_2_$NOW\",\"order_id\":\"order_2_$NOW\",\"amount\":780000,\"currency\":\"INR\",\"method\":\"card\",\"created_at\":$T2,\"error_reason\":\"payment_risk_check_failed\",\"error_source\":\"customer\"}}}}"

echo "3) a failure that is the merchant's own bug (order_3):"
send "evt_shadow_3_$NOW" "{\"event\":\"payment.failed\",\"created_at\":$T3,\"payload\":{\"payment\":{\"entity\":{\"id\":\"pay_shadow_3_$NOW\",\"order_id\":\"order_3_$NOW\",\"amount\":120000,\"currency\":\"INR\",\"method\":\"card\",\"created_at\":$T3,\"error_reason\":\"invalid_order_id\",\"error_source\":\"business\"}}}}"

echo "4) three hours later the first customer pays on their own - a NEW payment, same order:"
send "evt_shadow_4_$NOW" "{\"event\":\"payment.captured\",\"created_at\":$T4,\"payload\":{\"payment\":{\"entity\":{\"id\":\"pay_shadow_9_$NOW\",\"order_id\":\"order_1_$NOW\",\"amount\":249900,\"currency\":\"INR\",\"method\":\"upi\",\"created_at\":$T4}}}}"

echo "5) what shadow mode has seen:"
curl -s "$HOST/shadow/summary" -H "Authorization: Bearer $API_KEY" | python3 -m json.tool

echo "6) another system asks the contract before texting a customer it has already"
echo "   contacted three times this week:"
curl -s "$HOST/policy/check" -H "Authorization: Bearer $API_KEY" -H "content-type: application/json" \
  -d "{\"event_id\":\"pay_shadow_1_$NOW\",\"customer_id\":\"cust_demo\",\"action\":\"NUDGE\",\"channel\":\"sms\",\"template_id\":\"tpl_funds_01\",\"amount_paise\":249900,\"episode_started_at\":\"$(date -u -r "$T1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$T1" +%Y-%m-%dT%H:%M:%SZ)\",\"failure_reason\":\"insufficient_funds\",\"customer\":{\"consented_channels\":[\"sms\"],\"contacts_last_7d\":3}}" \
  | python3 -m json.tool

# The same question for a fresh customer, then the record catching an under-report.
STARTED=$(date -u -r "$T1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$T1" +%Y-%m-%dT%H:%M:%SZ)
SENT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CUSTOMER="cust_fresh_$NOW"
ASK="{\"event_id\":\"pay_fresh_$NOW\",\"customer_id\":\"$CUSTOMER\",\"action\":\"NUDGE\",\"channel\":\"sms\",\"template_id\":\"tpl_funds_01\",\"amount_paise\":249900,\"episode_started_at\":\"$STARTED\",\"failure_reason\":\"insufficient_funds\",\"customer\":{\"consented_channels\":[\"sms\"],\"contacts_last_7d\":0}}"

ask() {
  curl -s "$HOST/policy/check" -H "Authorization: Bearer $API_KEY" \
       -H "content-type: application/json" -d "$ASK"
}

echo "7) a customer nobody has contacted - allowed, and the slot is now held:"
CHECK_ID=$(ask | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['check_id']); print('   allowed:', r['allowed'], file=sys.stderr)")
echo "   before it confirms, a second vendor asks about the same customer:"
ask | python3 -c "import json,sys; r=json.load(sys.stdin); print('   allowed:', r['allowed']); [print('  ', b['rule_id'], '-', b['reason']) for b in r['blocked_by']]"
echo "   the first caller confirms it sent the SMS:"
curl -s "$HOST/policy/contacts" -H "Authorization: Bearer $API_KEY" -H "content-type: application/json" \
  -d "{\"event_id\":\"pay_fresh_$NOW\",\"customer_id\":\"$CUSTOMER\",\"channel\":\"sms\",\"contacted_at\":\"$SENT\",\"check_id\":\"$CHECK_ID\",\"contact_ref\":\"sms_$NOW\"}"; echo

echo "8) a minute later the caller asks again, still claiming zero contacts this week:"
ask | python3 -c "import json,sys; r=json.load(sys.stdin); print(json.dumps({k: r[k] for k in ('allowed','blocked_by','history')}, indent=4))"

echo "9) that customer's contact record, judged against the contract:"
curl -s "$HOST/policy/customers/$CUSTOMER/contacts" -H "Authorization: Bearer $API_KEY" | python3 -m json.tool
