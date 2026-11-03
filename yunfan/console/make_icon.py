"""生成控制台图标（一次性脚本，产物 gui/icon.ico）。"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 底板
d.rounded_rectangle([10, 10, S - 10, S - 10], radius=46, fill=(14, 17, 24, 255),
                    outline=(79, 140, 255, 255), width=5)
# 芯片本体
d.rounded_rectangle([80, 80, S - 80, S - 80], radius=14, fill=(23, 35, 58, 255),
                    outline=(110, 168, 255, 255), width=3)
# 引脚
for i, pos in enumerate((100, 124, 148, 172)):
    d.rectangle([62, pos - 4, 80, pos + 4], fill=(79, 140, 255, 255))
    d.rectangle([S - 80, pos - 4, S - 62, pos + 4], fill=(79, 140, 255, 255))
    d.rectangle([pos - 4, 62, pos + 4, 80], fill=(79, 140, 255, 255))
    d.rectangle([pos - 4, S - 80, pos + 4, S - 62], fill=(79, 140, 255, 255))
# 芯片内的数字波形
wave = [(96, 152), (114, 152), (114, 108), (132, 108), (132, 152), (150, 152), (150, 108), (168, 108)]
d.line(wave, fill=(51, 209, 155, 255), width=6, joint="curve")

out = Path(__file__).resolve().parent
png = out / "icon.png"
ico = out / "icon.ico"
img.save(png)
img.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", png, "and", ico)
