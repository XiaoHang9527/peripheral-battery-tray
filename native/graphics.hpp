#pragma once

#include <windows.h>

namespace pbt {

inline constexpr int ringWindowPixels = 48;
inline constexpr int ringGapPixels = 2;
inline constexpr int trayClearancePixels = 6;

struct Badge {
    int level = -1;
    bool charging = false;
    bool faded = false;
    bool disconnected = false;
    bool dark = false;
    bool low = false;
    bool operator==(const Badge&) const = default;
};

COLORREF batteryColor(const Badge& badge);
HICON batteryIcon(Badge badge, int size);
bool drawRing(HWND window, POINT point, int size, Badge badge, BYTE alpha = 255);

struct Taskbar {
    HWND window{};
    RECT bounds{}, tray{};
    bool vertical = false;
};

Taskbar findTaskbar();
bool coveredByTaskbar(HWND ring, HWND taskbar);
bool fullscreenOn(const RECT& taskbar);

} // namespace pbt
