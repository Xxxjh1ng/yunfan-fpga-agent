"""无 API Key 演示：用内置参考实现顶替大模型输出，跑通编排层 + 真实 EDA 闭环。

用途
----
没有模型 Key 时，用来验证「规格 → 门控 → Testbench → 仿真 → 综合 → 布局布线 → 比特流」
这条流水线、以及运行产物长什么样。

⚠️ 这不是「AI 生成」：RTL 来自 examples/<设计>/ref.v。
   接入真实 Key 后把 FixtureModel 换成 OpenAICompatModel 即可，其余代码完全一致。

用法
----
    .\.venv\Scripts\python.exe scripts\demo_without_key.py counter
    .\.venv\Scripts\python.exe scripts\demo_without_key.py crc32
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from yunfan.config import Settings
from yunfan.llm import TokenLedger
from yunfan.llm.offline import FixtureModel
from yunfan.orchestration.pipeline import Pipeline, PipelineConfig
from yunfan.orchestration.spec import DesignSpec

design = sys.argv[1] if len(sys.argv) > 1 else "counter"

spec = DesignSpec.load(ROOT / "examples" / design / "spec.json")
ref_rtl = (ROOT / "examples" / design / "ref.v").read_text(encoding="utf-8")

model = FixtureModel({f"rtl-{i}": ref_rtl for i in (1, 2, 3)}, ledger=TokenLedger())
settings = Settings.build()
settings.out_dir = ROOT / "runs"

pipe = Pipeline(settings, model, config=PipelineConfig(mode="full"), verbose=True)
rec = pipe.run(user_text=None, spec=spec, seed=0)

print("-" * 72)
print(rec.brief())
print("产物目录:", Path(settings.out_dir) / rec.run_id)
