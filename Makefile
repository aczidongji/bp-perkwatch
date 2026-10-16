# 所有目标都通过 uv 调用项目内 .venv，不依赖系统 Python。
UV := $(HOME)/.local/bin/uv
BP := uv run bp

.PHONY: help init install collect analyse verify run tick report doctor \
        test lint timers check-sources mail-test clean

help:
	@echo "初始化        make init            # venv+依赖+建库+来源清单+.env"
	@echo "单次采集      make collect         # 不调用模型"
	@echo "单次全流程    make run             # 采集+分析+核验"
	@echo "分析积压      make analyse"
	@echo "官方核验      make verify"
	@echo "日报预览      make report"
	@echo "定时器安装    make timers"
	@echo "校验来源      make check-sources"
	@echo "测试          make test"
	@echo "自检          make doctor"

init:
	bash scripts/bootstrap.sh

install:
	$(UV) sync --extra dev

collect:
	$(BP) collect

analyse:
	$(BP) analyse

verify:
	$(BP) verify

run:
	$(BP) run

tick:
	$(BP) tick

report:
	$(BP) report --stdout

doctor:
	$(BP) doctor

check-sources:
	$(BP) sources check

mail-test:
	$(BP) mail-test

timers:
	bash scripts/install-timers.sh

test:
	$(UV) run pytest -v

clean:
	rm -rf .pytest_cache **/__pycache__ .coverage
