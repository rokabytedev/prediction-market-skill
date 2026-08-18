SKILL_NAME := prediction-market
CC_SKILLS  := $(HOME)/.claude/skills
DIST       := dist

.PHONY: test test-live install uninstall zip clean all

all: test install zip

test:
	python3 -m unittest discover -s tests -p 'test_pm_query.py' -v

test-live:
	python3 tests/test_live.py

install:
	rm -rf "$(CC_SKILLS)/$(SKILL_NAME)"
	mkdir -p "$(CC_SKILLS)"
	cp -r skills/$(SKILL_NAME) "$(CC_SKILLS)/$(SKILL_NAME)"
	find "$(CC_SKILLS)/$(SKILL_NAME)" -name '__pycache__' -type d -exec rm -rf {} +
	@echo "installed → $(CC_SKILLS)/$(SKILL_NAME)"

uninstall:
	rm -rf "$(CC_SKILLS)/$(SKILL_NAME)"

zip:
	rm -rf "$(DIST)"
	mkdir -p "$(DIST)/$(SKILL_NAME)"
	cp -r skills/$(SKILL_NAME)/. "$(DIST)/$(SKILL_NAME)/"
	find "$(DIST)" -name '__pycache__' -type d -exec rm -rf {} +
	cd "$(DIST)" && zip -qr "$(SKILL_NAME).zip" "$(SKILL_NAME)"
	rm -rf "$(DIST)/$(SKILL_NAME)"
	@echo "packaged → $(DIST)/$(SKILL_NAME).zip  (upload at claude.ai → Settings → Capabilities → Skills)"

clean:
	rm -rf "$(DIST)"
	find . -name '__pycache__' -type d -exec rm -rf {} +
