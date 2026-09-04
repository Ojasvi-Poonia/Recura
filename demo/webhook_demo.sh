#!/usr/bin/env bash
# Live webhook demo for the video. Run `make serve` in another tab first.
export RAZORPAY_WEBHOOK_SECRET=whsec_demo_for_video
BODY='{"event":"payment.failed","created_at":1756000000,"payload":{"payment":{"entity":{"id":"pay_demo1","amount":249900,"currency":"INR","created_at":1756000000,"error_reason":"insufficient_funds"}}}}'
SIG=$(python3 -c "import hmac,hashlib,sys;print(hmac.new(b'whsec_demo_for_video',sys.argv[1].encode(),hashlib.sha256).hexdigest())" "$BODY")
echo "1) valid signature:"
curl -s -X POST localhost:8000/webhooks/razorpay -H "x-razorpay-signature: $SIG" -H "x-razorpay-event-id: evt_demo1" -d "$BODY"; echo
echo "2) replayed (idempotency):"
curl -s -X POST localhost:8000/webhooks/razorpay -H "x-razorpay-signature: $SIG" -H "x-razorpay-event-id: evt_demo1" -d "$BODY"; echo
echo "3) tampered body:"
curl -s -X POST localhost:8000/webhooks/razorpay -H "x-razorpay-signature: $SIG" -H "x-razorpay-event-id: evt_demo2" -d "${BODY/249900/9999900}"; echo
