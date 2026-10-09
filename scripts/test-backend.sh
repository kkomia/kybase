#!/usr/bin/env sh
# 后端测试（**只在需要跑 pytest 时用它**；完整门禁是 scripts/check-backend.sh）
#
# 与门禁的区别只有一处：**没设 KYLAB_TEST_DATABASE_URL 时它会从 backend/.env 借**。
#
# 为什么可以借：测试夹具（`backend/tests/conftest.py` 的 `pg_database`）只把这个 DSN
# 当**维护连接**用——它建一个 `kylab_test_<随机>` 的**临时库**、建好 schema 跑用例，
# 结束时 `drop database ... with (force)` 删掉。生产/开发库本身**只被连一次，不会被写**
# （用例里的每一次清库都落在那个临时库上，夹具还会把应用的 KYLAB_DATABASE_URL 一起
# 改指过去）。
#
# 但"借"这件事必须**说出来**：门禁里那条"没有测试库就报红"的纪律是针对"静默变绿"的，
# 不是针对"指向一个真实的库"——所以这里会把"借的是哪台主机、哪个库"打在屏幕上。
#
# 用法：
#   sh scripts/test-backend.sh                 # 借 .env 里的库（没设环境变量时）
#   KYLAB_TEST_DATABASE_URL=... sh scripts/test-backend.sh   # 显式指定
#   sh scripts/test-backend.sh -q -k 某个用例名   # 额外参数原样传给 pytest
#   KYLAB_TEST_TARGETS=tests/unit sh scripts/test-backend.sh  # 只跑一部分（默认 tests）
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT/backend/.env"

VENV_PY="$ROOT/backend/.venv/Scripts/python.exe"
[ -x "$VENV_PY" ] || VENV_PY="$ROOT/backend/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "找不到 venv 解释器：$VENV_PY" >&2
    echo "先建虚拟环境并装依赖（见 backend/README.md）" >&2
    exit 2
fi

DSN="${KYLAB_TEST_DATABASE_URL:-}"
if [ -z "$DSN" ]; then
    # 只取 `KYLAB_DATABASE_URL=` 那一段；**不打印它的口令部分**
    DSN=$(sed -n 's/^KYLAB_DATABASE_URL=//p' "$ENV_FILE" 2>/dev/null | head -n 1)
    if [ -z "$DSN" ]; then
        echo "未设置 KYLAB_TEST_DATABASE_URL，且 $ENV_FILE 里没有 KYLAB_DATABASE_URL" >&2
        echo "请显式指定一个带 pgvector 的库（夹具会建/删临时库）：" >&2
        echo "  KYLAB_TEST_DATABASE_URL=postgresql://用户:口令@主机:5432/postgres \\" >&2
        echo "    sh scripts/test-backend.sh" >&2
        exit 2
    fi
    # 只露主机名与库名：口令留在变量里
    SAFE=$(printf '%s' "$DSN" | sed -e 's|^[^:]*://[^@]*@||')
    echo "==> 借 $ENV_FILE 里的库作**维护连接**：$SAFE"
    echo "    夹具会建临时库 kylab_test_<随机> 跑完即删；这个库本身不会被写。"
fi

cd "$ROOT/backend" || exit 2
# 目标用环境变量给（默认整个 tests/）：写成位置参数的话，它会和默认的 tests
# **叠在一起**（既跑全量又跑那一个文件），所以这里不做"猜参数"的事。
# shellcheck disable=SC2086
KYLAB_TEST_DATABASE_URL="$DSN" "$VENV_PY" -m pytest ${KYLAB_TEST_TARGETS:-tests} \
    -m 'not bench and not cloud' -q "$@"
