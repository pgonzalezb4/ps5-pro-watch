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
	cp launchd/*.plist ~/Library/LaunchAgents/
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
