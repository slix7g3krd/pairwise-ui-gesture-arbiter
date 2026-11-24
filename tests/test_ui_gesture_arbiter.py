import pytest
import gameplay.ui_gesture_arbiter as mod

# 注意：(1) 是整数而非元组，fn(*1) 会抛 TypeError；这里必须是一元组 (1,)。
CASES = [
    ("gesture_arbiter", (1,)),
    ("long_press_threshold", (1,)),
    ("focus_restore", (1,)),
    ("nav_touch_unify", (1,)),
    ("coord_remap", (1,)),
    ("bubble_order", (1,)),
    ("scroll_conflict", (1,)),
    ("validation", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("ime_focus", (1,)),
    ("metrics", (1,)),
    ("gamepad_focus", (1,)),
    ("rotate", (1,)),
    ("determinism", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
