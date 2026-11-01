"""B15 / F7-A: virtual-surface crops, bounds, and a topology change."""
import importlib.util
import io
import sys
import types
from pathlib import Path

from PIL import Image, ImageGrab


class FakeModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = lambda *args, **kwargs: None
        setattr(self, name, value)
        return value


def load_hands(monkeypatch):
    for name in ("pyautogui", "tkinter", "tkinter.messagebox"):
        monkeypatch.setitem(sys.modules, name, FakeModule(name))
    path = Path(__file__).resolve().parents[1] / "hands/hands_relay_hands.py"
    spec = importlib.util.spec_from_file_location("b15_hands", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SHARED_SECRET = "s"
    return module


def paint(rects, colors):
    vx = min(rect["x"] for rect in rects)
    vy = min(rect["y"] for rect in rects)
    width = max(rect["x"] + rect["w"] for rect in rects) - vx
    height = max(rect["y"] + rect["h"] for rect in rects) - vy
    image = Image.new("RGB", (width, height), (0, 0, 0))
    for rect, color in zip(rects, colors):
        image.paste(
            color,
            (rect["x"] - vx, rect["y"] - vy, rect["x"] - vx + rect["w"], rect["y"] - vy + rect["h"]),
        )
    return image


def install_surface(monkeypatch, module, rects, colors, image=None):
    virtual = image if image is not None else paint(rects, colors)
    primary = virtual.crop((0, 0, min(10, virtual.size[0]), min(10, virtual.size[1])))
    calls = []

    def screenshot(region=None, allScreens=False, **kwargs):
        calls.append(("pyautogui", region, allScreens))
        if allScreens:
            return virtual.copy()
        if region is not None:
            x, y, w, h = region
            return primary.crop((x, y, x + w, y + h))
        return primary.copy()

    def grab(bbox=None, all_screens=False, **kwargs):
        calls.append(("imagegrab", bbox, all_screens))
        return virtual.copy() if all_screens else primary.copy()

    monkeypatch.setattr(module, "_monitor_rects", lambda: [dict(rect) for rect in rects])
    monkeypatch.setattr(module.pyautogui, "screenshot", screenshot)
    monkeypatch.setattr(ImageGrab, "grab", grab)
    return virtual, calls


def png_pixel(response, x, y):
    image = Image.open(io.BytesIO(response.data))
    return image.getpixel((x, y)), image.size


def test_negative_origin_and_negative_y_crop_the_virtual_surface(monkeypatch):
    module = load_hands(monkeypatch)
    rects = [
        {"x": -100, "y": -40, "w": 100, "h": 40, "primary": False},
        {"x": 0, "y": 0, "w": 80, "h": 60, "primary": True},
    ]
    install_surface(monkeypatch, module, rects, [(0, 128, 0), (255, 0, 0)])
    left = module._grab(rects[0])
    assert left.getpixel((50, 20)) == (0, 128, 0)
    assert left.size == (100, 40)
    upper = module._grab({"x": -100, "y": -40, "w": 20, "h": 20})
    assert upper.getpixel((0, 0)) == (0, 128, 0)


def test_primary_not_at_index_zero_and_mixed_sizes(monkeypatch):
    module = load_hands(monkeypatch)
    rects = [
        {"x": -30, "y": 0, "w": 30, "h": 20, "primary": False},
        {"x": 0, "y": -10, "w": 50, "h": 40, "primary": True},
    ]
    _virtual, calls = install_surface(monkeypatch, module, rects, [(0, 0, 255), (255, 0, 0)])
    client = module.app.test_client()
    response = client.get("/screenshot?monitor=0", headers={"X-Secret": "s"})
    pixel, size = png_pixel(response, 10, 10)
    assert response.status_code == 200
    assert size == (30, 20)
    assert pixel == (0, 0, 255)
    assert response.headers["X-Screen-Monitor"] == "0"
    assert response.headers["X-Screen-Origin"] == "-30,-10"
    assert response.headers["X-Screen-Surface"] == "80,40"
    assert response.headers["X-Screen-Rect"] == "-30,0,30,20"
    assert calls[0][0] == "imagegrab" and calls[0][2] is True
    assert all(item[1] is None for item in calls)


def test_region_crossing_displays_keeps_both_colors(monkeypatch):
    module = load_hands(monkeypatch)
    rects = [
        {"x": -100, "y": 0, "w": 100, "h": 100, "primary": False},
        {"x": 0, "y": 0, "w": 100, "h": 100, "primary": True},
        {"x": 100, "y": 0, "w": 100, "h": 100, "primary": False},
    ]
    install_surface(
        monkeypatch, module, rects, [(0, 128, 0), (255, 0, 0), (0, 0, 255)]
    )
    client = module.app.test_client()
    response = client.get("/screenshot?region=-50,0,100,10", headers={"X-Secret": "s"})
    assert response.status_code == 200
    left, _size = png_pixel(response, 0, 0)
    right, size = png_pixel(response, 60, 0)
    assert size == (100, 10)
    assert left == (0, 128, 0)
    assert right == (255, 0, 0)
    assert response.headers["X-Screen-Region"] == "-50,0,100,10"


def test_out_of_surface_region_is_rejected_without_padding(monkeypatch):
    module = load_hands(monkeypatch)
    rects = [{"x": 0, "y": 0, "w": 100, "h": 80, "primary": True}]
    install_surface(monkeypatch, module, rects, [(255, 0, 0)])
    client = module.app.test_client()
    response = client.get("/screenshot?region=90,0,20,10", headers={"X-Secret": "s"})
    assert response.status_code == 400
    assert b"PNG" not in response.data
    assert "outside" in response.get_json()["error"]


def test_extent_mismatch_refuses_instead_of_padding(monkeypatch):
    module = load_hands(monkeypatch)
    rects = [
        {"x": -100, "y": 0, "w": 100, "h": 100, "primary": False},
        {"x": 0, "y": 0, "w": 100, "h": 100, "primary": True},
    ]
    short = Image.new("RGB", (100, 100), (0, 0, 0))
    install_surface(monkeypatch, module, rects, [(0, 128, 0), (255, 0, 0)], image=short)
    client = module.app.test_client()
    response = client.get("/screenshot?monitor=0", headers={"X-Secret": "s"})
    assert response.status_code == 409
    assert "extent" in response.get_json()["error"]
    assert b"PNG" not in response.data


def test_hot_plug_retries_once_then_uses_the_stable_topology(monkeypatch):
    module = load_hands(monkeypatch)
    first = [{"x": 0, "y": 0, "w": 40, "h": 40, "primary": True}]
    second = [
        {"x": -20, "y": 0, "w": 20, "h": 20, "primary": False},
        {"x": 0, "y": 0, "w": 20, "h": 30, "primary": True},
    ]
    reads = {"n": 0}

    def rects():
        reads["n"] += 1
        source = first if reads["n"] < 4 else second
        return [dict(rect) for rect in source]

    virtual = paint(second, [(0, 0, 255), (255, 0, 0)])
    monkeypatch.setattr(module, "_monitor_rects", rects)
    monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None, all_screens=False, **kw: virtual.copy())
    client = module.app.test_client()
    response = client.get("/screenshot?monitor=0", headers={"X-Secret": "s"})
    pixel, size = png_pixel(response, 0, 0)
    assert response.status_code == 200
    assert size == (20, 20)
    assert pixel == (0, 0, 255)
    assert response.headers["X-Screen-Origin"] == "-20,0"


def test_hot_plug_that_keeps_changing_is_refused(monkeypatch):
    module = load_hands(monkeypatch)
    reads = {"n": 0}

    def rects():
        reads["n"] += 1
        return [{"x": reads["n"], "y": 0, "w": 10, "h": 10, "primary": True}]

    monkeypatch.setattr(module, "_monitor_rects", rects)
    monkeypatch.setattr(
        ImageGrab, "grab", lambda bbox=None, all_screens=False, **kw: Image.new("RGB", (10, 10), (1, 2, 3))
    )
    response = module.app.test_client().get("/screenshot?region=0,0,10,10", headers={"X-Secret": "s"})
    assert response.status_code == 409
    assert "topology" in response.get_json()["error"]


def test_legacy_frame_does_not_request_the_virtual_surface(monkeypatch):
    module = load_hands(monkeypatch)
    calls = []
    monkeypatch.setattr(
        module.pyautogui, "screenshot", lambda region=None, allScreens=False, **kw: calls.append((region, allScreens)) or Image.new("RGB", (8, 8), (1, 1, 1))
    )
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: calls.append(("imagegrab", kw)) or Image.new("RGB", (8, 8)))
    image = module._grab(None)
    assert image.size == (8, 8)
    assert calls == [(None, False)]
