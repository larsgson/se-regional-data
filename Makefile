COUNTRY    ?= mx
TAG        ?= data-$(shell date -u +%Y.%m.%d)
ZSTD_LEVEL ?= 19

export COUNTRY TAG ZSTD_LEVEL

COUNTRY_UPPER := $(shell echo $(COUNTRY) | tr a-z A-Z)
COUNTRY_LOWER := $(shell echo $(COUNTRY) | tr A-Z a-z)

# Parse a conf file into a space-separated list of country codes
read-conf = $(shell sed 's/\#.*//' $(1) | tr '\n' ' ')

.PHONY: help release release-dry release-draft release-publish lang-names vrs-report gen-vrs pack notes \
        pipeline fetch dedupe map-figures map-media app-config classify classify-rescan clean \
        region-pipeline region-release merge-region merge-global \
        publish-cdn publish-cdn-dry prune-releases

help:
	@echo "Targets:"
	@echo "  make pipeline         fetch + dedupe + map-figures + map-media"
	@echo "  make classify         scan SE per iso → data/$(COUNTRY_LOWER)/licenses.json (disk-cached; fast on re-runs)"
	@echo "  make classify-rescan  drop the disk cache and rescan everything (~30 min on a cold cache)"
	@echo "  make release          classify + pack + diff + gh release create"
	@echo "  make release-dry      same but prints what would upload (no network)"
	@echo "  make release-draft    publish as a hidden draft on GitHub"
	@echo "  make release-publish  release + publish-cdn (GitHub release, then push to R2)"
	@echo "  make publish-cdn      upload data/$(COUNTRY_LOWER)/ to Cloudflare R2 (cdn.bibel.wiki/pkf/)"
	@echo "  make publish-cdn-dry  preview the CDN upload (rclone --dry-run, no writes)"
	@echo "  make pack             build release/*.tar.zst + index.json"
	@echo "  make notes            write release/release-notes.md"
	@echo "  make clean            rm -rf release/"
	@echo ""
	@echo "  make region-pipeline REGION=conf/central-america.conf"
	@echo "  make region-release  REGION=conf/central-america.conf"
	@echo "  make merge-region    REGION=conf/central-america.conf MERGED=latam"
	@echo "  make merge-global    CONF=conf/global.conf MERGED=global"
	@echo ""
	@echo "Vars: COUNTRY=$(COUNTRY)  TAG=$(TAG)  ZSTD_LEVEL=$(ZSTD_LEVEL)"
	@echo "Data: data/$(COUNTRY_LOWER)/"

release:
	scripts/release.sh

release-dry:
	DRY_RUN=1 scripts/release.sh

release-draft:
	DRAFT=1 scripts/release.sh

release-publish: release publish-cdn

prune-releases:
	scripts/prune_releases.sh

publish-cdn:
	scripts/publish-cdn.sh

publish-cdn-dry:
	DRY_RUN=1 scripts/publish-cdn.sh

lang-names:
	python3 scripts/build_lang_names.py

vrs-report:
	python3 scripts/analyze_versification.py

gen-vrs:
	python3 scripts/gen_vrs.py

pack: gen-vrs
	python3 scripts/pack_release.py

notes:
	python3 scripts/diff_manifest.py

pipeline:
ifeq ($(FORCE),1)
	$(MAKE) --no-print-directory fetch process
else ifneq ($(wildcard data/$(COUNTRY_LOWER)/manifest.json),)
	@echo "[pipeline] data/$(COUNTRY_LOWER)/ exists — skipping fetch (use FORCE=1 to re-fetch)"
	$(MAKE) --no-print-directory process
else
	$(MAKE) --no-print-directory fetch process
endif

process: dedupe map-figures map-media app-config

fetch:
	python3 scripts/fetch_pkf.py --country $(COUNTRY_UPPER) --workers 8

dedupe:
	python3 scripts/dedupe_assets.py

map-figures:
	node scripts/map_figures.mjs

map-media:
	CONCURRENCY=6 python3 scripts/map_media.py

app-config:
	CONCURRENCY=8 python3 scripts/map_app_config.py

classify:
	python3 scripts/classify_licenses.py

classify-rescan:
	rm -rf data/.license-scan-cache
	python3 scripts/classify_licenses.py

region-pipeline:
	@test -n "$(REGION)" || { echo "usage: make region-pipeline REGION=conf/<name>.conf"; exit 1; }
	@for cc in $(call read-conf,$(REGION)); do \
		echo ""; echo "======== $$cc ========"; \
		COUNTRY=$$cc $(MAKE) --no-print-directory pipeline || exit 1; \
	done

region-release:
	@test -n "$(REGION)" || { echo "usage: make region-release REGION=conf/<name>.conf"; exit 1; }
	@for cc in $(call read-conf,$(REGION)); do \
		echo ""; echo "======== $$cc ========"; \
		COUNTRY=$$cc TAG=data-$$cc-$(shell date -u +%Y.%m.%d) $(MAKE) --no-print-directory release || exit 1; \
	done

merge-region:
	@test -n "$(REGION)" || { echo "usage: make merge-region REGION=conf/<name>.conf MERGED=latam"; exit 1; }
	@test -n "$(MERGED)" || { echo "usage: make merge-region REGION=conf/<name>.conf MERGED=latam"; exit 1; }
	python3 scripts/merge_countries.py $(REGION) $(MERGED)
	COUNTRY=$(MERGED) $(MAKE) --no-print-directory classify

merge-global:
	@test -n "$(CONF)" || { echo "usage: make merge-global CONF=conf/global.conf MERGED=global"; exit 1; }
	python3 scripts/merge_countries.py $(CONF) $(or $(MERGED),global)
	COUNTRY=$(or $(MERGED),global) $(MAKE) --no-print-directory classify

clean:
	rm -rf release/
