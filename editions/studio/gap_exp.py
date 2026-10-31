import cv2, numpy as np
bg = cv2.imread("dbg_bg.jpg", cv2.IMREAD_COLOR)
piece = cv2.imread("dbg_piece.png", cv2.IMREAD_UNCHANGED)
alpha = piece[:,:,3]
pg = cv2.cvtColor(piece[:,:,:3], cv2.COLOR_BGR2GRAY)
bgg = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)

ys, xs = np.where(alpha > 50)
print("piece bbox in canvas: x", xs.min(), xs.max(), "y", ys.min(), ys.max())

# 裁出 piece 有效区域（bbox）
x0,x1,y0,y1 = xs.min(), xs.max()+1, ys.min(), ys.max()+1
piece_c = pg[y0:y1, x0:x1]; alpha_c = alpha[y0:y1, x0:x1]

methods = {}
# M1: 灰度模板+mask
r = cv2.matchTemplate(bgg, piece_c, cv2.TM_CCOEFF_NORMED, mask=alpha_c)
_,mv,_,ml = cv2.minMaxLoc(r); methods["gray+mask coeff"] = (ml[0], mv)
# M2: 边缘（不同阈值）
pe = cv2.Canny(alpha_c, 50, 150)
be = cv2.Canny(bgg, 50, 150)
r = cv2.matchTemplate(be, pe, cv2.TM_CCOEFF_NORMED)
_,mv,_,ml = cv2.minMaxLoc(r); methods["canny50 coeff"] = (ml[0], mv)
# M3: ccnorm on edges
r = cv2.matchTemplate(be, pe, cv2.TM_CCORR_NORMED)
_,mv,_,ml = cv2.minMaxLoc(r); methods["canny50 corr"] = (ml[0], mv)
# M4: 暗槽检测——背景减去中值模糊，找暗区
blur = cv2.medianBlur(bgg, 31)
diff = blur.astype(int) - bgg.astype(int)  # 暗槽处为正
dark = np.clip(diff, 0, 255).astype(np.uint8)
_, th = cv2.threshold(dark, 18, 255, cv2.THRESH_BINARY)
th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((9,9),np.uint8))
cnts,_ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
best = None
for c in cnts:
    X,Y,W,H = cv2.boundingRect(c)
    if 60 < W < 160 and 60 < H < 160 and X > 100:
        if best is None or cv2.contourArea(c) > best[4]:
            best = (X,Y,W,H,cv2.contourArea(c))
methods["dark-slot"] = (best[0] if best else -1, best[4] if best else -1)

for k,v in methods.items(): print(f"{k}: x={v[0]} score={v[1]:.3f}")

# 标注保存
vis = bg.copy()
for k,(X,sc) in methods.items():
    if X >= 0: cv2.rectangle(vis, (X,120), (X+110,230), (0,0,255), 2)
cv2.imwrite("dbg_vis.png", vis)
print("saved dbg_vis.png")