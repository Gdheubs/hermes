# Ready-to-run PR sequence for NousResearch/hermes-agent#85858
# NOTE: BananaAcc has push=false on upstream, so the PR must come from a FORK.
# This script is DRAFTED ONLY — not executed (per standing instruction).
# Review, then run from the hermes-agent repo root.

set -e

REPO_ROOT="C:/Users/enhal/AppData/Local/hermes/hermes-agent"
cd "$REPO_ROOT"

# 1. Create / ensure a personal fork exists (interactive once; safe to re-run).
gh repo fork NousResearch/hermes-agent --clone=false || true

# 2. Point a 'fork' remote at YOUR fork (BananaAccurate). Uses SSH or HTTPS per gh config.
gh repo set-default NousResearch/hermes-agent
git remote add fork "https://github.com/BananaAccurate/hermes-agent.git" 2>/dev/null || \
  git remote set-url fork "https://github.com/BananaAccurate/hermes-agent.git"

# 3. Branch + stage the four untracked artifacts.
git checkout -b fix/85858-concurrent-memory-clobber
git add \
  tools/memory_tool_85858_dataloss_fix.patch \
  tools/memory_tool_85858_lock_hardening.patch \
  tests/tools/test_memory_concurrency_85858.py \
  tools/memory_tool_85858_PR.md
git commit -m "Fix concurrent-instance memory clobber (last-writer-wins) #85858

Make MemoryStore.save_to_disk merge-live + tombstone instead of blind
overwrite, so two instances sharing one profile no longer lose each
other's entries or resurrect removed ones. Adds regression test."

# 4. Push to the FORK (not upstream).
git push -u fork fix/85858-concurrent-memory-clobber

# 5. Open the PR against upstream main from the fork branch.
gh pr create \
  --repo NousResearch/hermes-agent \
  --base main \
  --head BananaAccurate:fix/85858-concurrent-memory-clobber \
  --title "Fix concurrent-instance memory clobber (last-writer-wins) #85858" \
  --body-file tools/memory_tool_85858_pr_body.md

echo "PR opened. Link above."
