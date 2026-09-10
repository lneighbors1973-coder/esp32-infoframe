# /// script
# dependencies = ["numpy"]
# ///
import json, numpy as np
pts = json.load(open("touchcal.json"))
rx = np.array([p["rx"] for p in pts], float)
ry = np.array([p["ry"] for p in pts], float)
sx = np.array([p["sx"] for p in pts], float)
sy = np.array([p["sy"] for p in pts], float)

print("correlation check (which raw axis drives which screen axis):")
print("  sx vs rx %+.3f   sx vs ry %+.3f" % (np.corrcoef(sx,rx)[0,1], np.corrcoef(sx,ry)[0,1]))
print("  sy vs rx %+.3f   sy vs ry %+.3f" % (np.corrcoef(sy,rx)[0,1], np.corrcoef(sy,ry)[0,1]))

# simple 2-parameter fit, axes transposed: sx <- ry, sy <- rx
ax, bx = np.polyfit(ry, sx, 1)
ay, by = np.polyfit(rx, sy, 1)
px, py = ax*ry + bx, ay*rx + by
r_simple = np.hypot(px-sx, py-sy)

# full affine, allows for panel skew
A = np.column_stack([rx, ry, np.ones_like(rx)])
cx, *_ = np.linalg.lstsq(A, sx, rcond=None)
cy, *_ = np.linalg.lstsq(A, sy, rcond=None)
qx, qy = A@cx, A@cy
r_affine = np.hypot(qx-sx, qy-sy)

print("\nsimple (transposed linear):  max err %.1f px   mean %.1f px" % (r_simple.max(), r_simple.mean()))
print("full affine:                 max err %.1f px   mean %.1f px" % (r_affine.max(), r_affine.mean()))
print("\nper-point residual (simple):")
for i,p in enumerate(pts):
    print("   screen(%3d,%3d) -> predicted(%6.1f,%6.1f)  err %.1f px" % (p["sx"],p["sy"],px[i],py[i],r_simple[i]))

print("\n--- constants for main.py (simple fit) ---")
print("TX_A, TX_B = %.6f, %.3f   # screen x = TX_A*raw_y + TX_B" % (ax, bx))
print("TY_A, TY_B = %.6f, %.3f   # screen y = TY_A*raw_x + TY_B" % (ay, by))
