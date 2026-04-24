COUNTRY    ?= mx
TAG        ?= data-$(shell date -u +%Y.%m.%d)
ZSTD_LEVEL ?= 19

export COUNTRY TAG ZSTD_LEVEL

COUNTRY_UPPER := $(shell echo $(COUNTRY) | tr a-z A-Z)

.PHONY: help release release-dry release-draft pack notes \
        pipeline fetch dedupe map-figures map-media classify classify-rescan clean

help:
	@echo "Targets:"
	@echo "  make pipeline         fetch + dedupe + map-figures + map-media"
	@echo "  make classify         scan SE per iso → data/pkf/licenses.json (disk-cached; fast on re-runs)"
	@echo "  make classify-rescan  drop the disk cache and rescan everything (~30 min on a cold cache)"
	@echo "  make release          classify + pack + diff + gh release create"
	@echo "  make release-dry      same but prints what would upload (no network)"
	@echo "  make release-draft    publish as a hidden draft on GitHub"
	@echo "  make pack             build release/*.tar.zst + index.json"
	@echo "  make notes            write release/release-notes.md"
	@echo "  make clean            rm -rf release/"
	@echo ""
	@echo "Vars: COUNTRY=$(COUNTRY)  TAG=$(TAG)  ZSTD_LEVEL=$(ZSTD_LEVEL)"

release:
	scripts/release.sh

release-dry:
	DRY_RUN=1 scripts/release.sh

release-draft:
	DRAFT=1 scripts/release.sh

pack:
	node scripts/pack_release.mjs

notes:
	node scripts/diff_manifest.mjs

pipeline: fetch dedupe map-figures map-media

fetch:
	python3 scripts/fetch_pkf.py --country $(COUNTRY_UPPER) --workers 8

dedupe:
	python3 scripts/dedupe_assets.py

map-figures:
	node scripts/map_figures.mjs

map-media:
	CONCURRENCY=6 node scripts/map_media.mjs

classify:
	node scripts/classify_licenses.mjs

classify-rescan:
	rm -rf data/.license-scan-cache
	node scripts/classify_licenses.mjs

clean:
	rm -rf release/
