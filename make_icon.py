import sys
from PIL import Image, ImageDraw, ImageFont
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), 56, fill=(22, 27, 38, 255), outline=(79, 140, 255, 255), width=10)
f = ImageFont.truetype("seguisb.ttf", 92)
d.text((S / 2, 92), "EN", font=f, fill=(243, 244, 246), anchor="mm")
d.text((S / 2, 178), "RU", font=f, fill=(79, 140, 255), anchor="mm")
img.save(sys.argv[1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
