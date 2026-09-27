"""滑块缺口识别（OpenCV 边缘模板匹配，替代 ddddocr/onnxruntime）。

字节滑块：背景 jpeg 上有凹槽阴影，滑块块 png 带透明通道。
用滑块块 alpha 轮廓做模板，在背景 Canny 边缘图上匹配，返回缺口左缘 x（背景自然坐标）。
"""
import cv2
import numpy as np


def find_gap_x(bg_bytes: bytes, piece_bytes: bytes) -> tuple:
    """返回 (gap_x, confidence)。gap_x 为背景图自然像素坐标下的缺口左缘。"""
    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if piece is None or bg is None:
        raise ValueError("图像解码失败")

    if piece.shape[2] == 4:
        alpha = piece[:, :, 3]
    else:
        alpha = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)

    piece_edge = cv2.Canny(alpha, 100, 200)
    bg_edge = cv2.Canny(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), 100, 200)

    # 模板不能比背景大
    if piece_edge.shape[0] > bg_edge.shape[0] or piece_edge.shape[1] > bg_edge.shape[1]:
        raise ValueError("滑块块比背景大，尺寸异常")

    res = cv2.matchTemplate(bg_edge, piece_edge, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return int(max_loc[0]), float(max_val)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    bg = Path(sys.argv[1] if len(sys.argv) > 1 else "dbg_bg.jpg").read_bytes()
    piece = Path(sys.argv[2] if len(sys.argv) > 2 else "dbg_piece.png").read_bytes()
    x, conf = find_gap_x(bg, piece)
    print(f"gap_x={x} conf={conf:.3f}")