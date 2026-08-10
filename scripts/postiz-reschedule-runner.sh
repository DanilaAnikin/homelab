#!/bin/sh
n=0; ok=0; fail=0
while IFS="|" read pid tq org; do
  [ -z "$pid" ] && continue
  n=$((n+1))
  if temporal workflow start --address postiz-temporal:7233 --namespace default \
     --task-queue main --type postWorkflowV106 \
     --workflow-id "post_$pid" --id-conflict-policy TerminateExisting \
     --input "{\"taskQueue\":\"$tq\",\"postId\":\"$pid\",\"organizationId\":\"$org\"}" >/dev/null 2>&1; then
    ok=$((ok+1))
  else
    fail=$((fail+1)); echo "FAIL $pid ($tq)"
  fi
done < /tmp/postiz-reschedule.txt
echo "TOTAL=$n OK=$ok FAIL=$fail"
