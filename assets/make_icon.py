from pathlib import Path
from PIL import Image, ImageDraw

icon=Image.new('RGBA',(256,256),(233,238,242,255))
draw=ImageDraw.Draw(icon)
draw.rounded_rectangle((40,28,216,228),radius=13,fill='white',outline='#236ca5',width=8)
draw.rectangle((58,49,198,172),fill='#aecbdf')
draw.polygon([(58,172),(112,93),(143,133),(163,108),(198,172)],fill='#4f87ae')
draw.line((151,204,225,130),fill='#b67a2e',width=12)
draw.ellipse((139,195,163,218),fill='#236ca5')
draw.polygon([(205,40),(212,59),(231,66),(212,73),(205,92),(198,73),(179,66),(198,59)],fill='#d4a652')
icon.save(Path(__file__).parent/'polascan.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
