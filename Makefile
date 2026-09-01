VENV=./.venv/bin
.PHONY: install scan digest discover test chat-id watch schedule unschedule logs
install:
	python3 -m venv .venv && $(VENV)/pip install -q -r requirements.txt && $(VENV)/python -m playwright install chromium && $(VENV)/python -m patchright install chromium
scan:      ; $(VENV)/python -m watcher scan
digest:    ; $(VENV)/python -m watcher digest
discover:  ; $(VENV)/python -m watcher discover
test:      ; $(VENV)/python -m watcher test
chat-id:   ; $(VENV)/python -m watcher find-chat-id
ca:        ; $(VENV)/python -m watcher scan --only CA
us:        ; $(VENV)/python -m watcher scan --only US
schedule:
	@mkdir -p ~/Library/LaunchAgents
	@for f in launchd/*.plist; do \
	   sed "s|__HOME__|$$HOME|g" "$$f" > ~/Library/LaunchAgents/$$(basename $$f); \
	 done
	launchctl unload ~/Library/LaunchAgents/com.pablo.ps5watch.scan.plist 2>/dev/null || true
	launchctl unload ~/Library/LaunchAgents/com.pablo.ps5watch.digest.plist 2>/dev/null || true
	launchctl load  ~/Library/LaunchAgents/com.pablo.ps5watch.scan.plist
	launchctl load  ~/Library/LaunchAgents/com.pablo.ps5watch.digest.plist
	@echo "scheduled: scan every 10 min, digest daily at 09:00"
unschedule:
	launchctl unload ~/Library/LaunchAgents/com.pablo.ps5watch.*.plist 2>/dev/null || true
logs: ; tail -f /tmp/ps5watch.*.log
stealth-check:
	@echo "verifying the stealth tier is invisible AND still passing..."
	@$(VENV)/python -m watcher scan --only Walmart
	@osascript -e 'tell application "System Events" to get name of every window of (every process whose name contains "Chromium")' 2>/dev/null \
	  | grep -q . && echo "⚠️  a Chromium window is on-screen" || echo "✅ no on-screen Chromium window"

# ---- GitHub Actions (cloud) ----
REPO=pgonzalezb4/ps5-pro-watch

ci-secrets:   ## push every non-empty var from .env to GitHub Actions secrets
	@grep -E '^[A-Z_]+=.+' .env | grep -vE '^\s*#' \
	  | grep -vE '=(\s*)$$' \
	  | grep -vE 'AAExampleTokenFromBotFather' \
	  | while IFS='=' read -r k v; do \
	      printf '%s' "$$v" | gh secret set "$$k" --repo $(REPO) >/dev/null \
	        && echo "  set $$k"; \
	    done
	@echo "secrets now on $(REPO):"; gh secret list --repo $(REPO)

ci-run:       ## trigger a digest run in the cloud right now
	gh workflow run watch.yml --repo $(REPO) -f mode=digest
	@sleep 6 && gh run list --repo $(REPO) --limit 3

ci-watch:     ## follow the latest cloud run
	gh run watch --repo $(REPO) $$(gh run list --repo $(REPO) --limit 1 --json databaseId -q '.[0].databaseId')

ci-log:       ## show the last run's scan output
	gh run view --repo $(REPO) --log $$(gh run list --repo $(REPO) --limit 1 --json databaseId -q '.[0].databaseId') 2>/dev/null | grep -E "^\s*[🟢⚪🟡⚫🚫🔴]|targets in|IN STOCK" | head -70

ci-off:       ## pause the cloud schedule
	gh workflow disable watch.yml --repo $(REPO)
ci-on:        ## resume the cloud schedule
	gh workflow enable watch.yml --repo $(REPO)
