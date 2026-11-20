#include "graphics.hpp"

#include <wtypes.h>
#include <gdiplus.h>
#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

namespace pbt {
namespace {

Gdiplus::Color color(COLORREF value, BYTE alpha = 255) {
    return Gdiplus::Color(alpha, GetRValue(value), GetGValue(value), GetBValue(value));
}

constexpr auto green = RGB(46, 204, 113);
constexpr auto amber = RGB(245, 166, 35);
constexpr auto red = RGB(255, 77, 79);
constexpr auto blue = RGB(59, 157, 255);
constexpr auto grey = RGB(140, 140, 140);

COLORREF ringColor(const Badge& badge) {
    if (badge.level < 0) return grey;
    if (badge.charging) return blue;
    if (badge.low) return red;
    return badge.level >= 60 ? green : amber;
}

void drawRingLabel(Gdiplus::Graphics& paint, int size, float scale, const Badge& badge) {
    using namespace Gdiplus;
    const std::wstring label = badge.level < 0 ? L"--" : std::to_wstring(badge.level);
    const FontFamily semibold(L"Segoe UI Semibold");
    const FontFamily fallback(L"Segoe UI");
    const auto* font = semibold.IsAvailable() ? &semibold : &fallback;
    GraphicsPath text;
    text.AddString(label.c_str(), static_cast<INT>(label.size()), font, FontStyleRegular,
                   13.f * scale, PointF{}, StringFormat::GenericTypographic());
    RectF inkBounds{};
    if (text.GetBounds(&inkBounds) != Ok || inkBounds.IsEmptyArea()) return;
    Matrix placement;
    placement.Translate((size - inkBounds.Width) / 2 - inkBounds.X, (size - inkBounds.Height) / 2 - inkBounds.Y);
    text.Transform(&placement);
    const auto ink = badge.level >= 0 && badge.charging ? blue : badge.level < 0
        ? (badge.dark ? RGB(168, 168, 168) : RGB(95, 95, 95))
        : (badge.dark ? RGB(255, 255, 255) : RGB(26, 26, 26));
    SolidBrush brush(color(ink, badge.faded || badge.disconnected ? 150 : 255));
    const auto pixelOffset = paint.GetPixelOffsetMode();
    paint.SetPixelOffsetMode(PixelOffsetModeHalf);
    paint.FillPath(&brush, &text);
    paint.SetPixelOffsetMode(pixelOffset);
}

void drawChargeBadge(Gdiplus::Graphics& paint, float size, float inset, float outline, bool dark) {
    using namespace Gdiplus;
    const REAL radius = (size - 2 * inset) * 7.f / 44.f;
    const REAL center = size - inset - radius - outline * .5f;
    const REAL unit = radius * .78f;
    const PointF points[] = {{center + unit * .18f, center - unit},
                             {center - unit * .62f, center + unit * .14f},
                             {center - unit * .06f, center + unit * .14f},
                             {center - unit * .18f, center + unit},
                             {center + unit * .62f, center - unit * .14f},
                             {center + unit * .06f, center - unit * .14f}};
    const RectF bounds(center - radius, center - radius, 2 * radius, 2 * radius);
    SolidBrush background(color(dark ? RGB(31, 31, 31) : RGB(243, 243, 243)));
    SolidBrush bolt(color(blue));
    Pen rim(color(blue), outline);
    paint.FillEllipse(&background, bounds);
    paint.DrawEllipse(&rim, bounds);
    paint.FillPolygon(&bolt, points, static_cast<INT>(std::size(points)));
}

class Surface {
public:
    explicit Surface(int size) : dc(CreateCompatibleDC(nullptr)), size(size) {
        if (!dc) return;
        BITMAPINFO info{};
        info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        info.bmiHeader.biWidth = size;
        info.bmiHeader.biHeight = -size;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &pixels, nullptr, 0);
        if (bitmap) previous = SelectObject(dc, bitmap);
    }
    ~Surface() {
        if (previous) SelectObject(dc, previous);
        if (bitmap) DeleteObject(bitmap);
        if (dc) DeleteDC(dc);
    }
    void draw(const Badge& badge, bool label, BYTE alpha = 255) const {
        using namespace Gdiplus;
        if (!bitmap) return;
        Bitmap image(size, size, size * 4, PixelFormat32bppPARGB, static_cast<BYTE*>(pixels));
        Graphics paint(&image);
        paint.SetSmoothingMode(SmoothingModeAntiAlias);
        // Keep the ring's clear center clickable without obscuring the taskbar.
        paint.Clear(label ? Color(1, 0, 0, 0) : Color(0, 0, 0, 0));
        const REAL scale = static_cast<REAL>(size) / ringWindowPixels;
        const REAL stroke = label ? 4.f * scale : static_cast<REAL>(size) / 8;
        const REAL inset = label ? 7.f * scale : stroke + size / 64.f;
        const REAL extent = static_cast<REAL>(size) - inset * 2;
        const bool faded = badge.faded || badge.disconnected;
        const auto fade = static_cast<BYTE>(faded ? alpha * 150 / 255 : alpha);
        const auto trackAlpha = static_cast<BYTE>((badge.dark ? 46 : 38) * fade / 255);
        Pen track(color(badge.dark ? RGB(255, 255, 255) : RGB(0, 0, 0), trackAlpha), stroke);
        paint.DrawEllipse(&track, inset, inset, extent, extent);
        if (badge.level > 0) {
            const auto progress = color(ringColor(badge), fade);
            Pen arc(progress, stroke);
            if (badge.level == 100) paint.DrawEllipse(&arc, inset, inset, extent, extent);
            else {
                const REAL sweep = badge.level * 3.6f;
                paint.DrawArc(&arc, inset, inset, extent, extent, -90.f, sweep);
                SolidBrush caps(progress);
                const REAL half = stroke / 2;
                const REAL radius = extent / 2 + half;
                for (const REAL degrees : {-90.f, sweep - 90.f}) {
                    const REAL angle = degrees * .01745329252f;
                    paint.FillEllipse(&caps, size / 2.f + radius * std::cos(angle) - half,
                                      size / 2.f + radius * std::sin(angle) - half, stroke, stroke);
                }
            }
        }
        if (badge.charging && !faded) drawChargeBadge(paint, static_cast<REAL>(size), label ? 2 * scale : 0, scale, badge.dark);
        if (badge.disconnected) {
            Pen slash(color(grey, static_cast<BYTE>(200 * fade / 255)), std::max(1.f, stroke * .65f));
            paint.DrawLine(&slash, size * .1875f, size * .8125f, size * .8125f, size * .1875f);
        }
        if (label) drawRingLabel(paint, size, scale, badge);
    }
    HDC dc{};
    HBITMAP bitmap{};
    HGDIOBJ previous{};
    void* pixels{};
    int size;
};

struct TrayLeft {
    RECT tray;
    LONG left;
};

BOOL CALLBACK includeTrayChildren(HWND child, LPARAM value) {
    auto& region = *reinterpret_cast<TrayLeft*>(value);
    RECT bounds{};
    if (IsWindowVisible(child) && GetWindowRect(child, &bounds) && bounds.right > bounds.left
        && bounds.top < region.tray.bottom && bounds.bottom > region.tray.top) {
        region.left = std::min(region.left, bounds.left);
    }
    return TRUE;
}

} // namespace

COLORREF batteryColor(const Badge& badge) {
    switch (const auto ink = ringColor(badge); ink) {
    case green: return badge.dark ? ink : RGB(19, 117, 65);
    case amber: return badge.dark ? ink : RGB(137, 82, 0);
    case red: return badge.dark ? RGB(255, 127, 129) : RGB(164, 38, 44);
    case blue: return badge.dark ? RGB(105, 178, 255) : RGB(29, 91, 167);
    default: return badge.dark ? ink : RGB(95, 95, 95);
    }
}

HICON batteryIcon(Badge badge, int size) {
    Surface drawing(size);
    if (!drawing.bitmap) return nullptr;
    drawing.draw(badge, false);
    const std::vector<BYTE> maskBits(static_cast<std::size_t>(((size + 15) / 16) * 2 * size));
    HBITMAP mask = CreateBitmap(size, size, 1, 1, maskBits.data());
    if (!mask) return nullptr;
    ICONINFO icon{TRUE, 0, 0, mask, drawing.bitmap};
    HICON result = CreateIconIndirect(&icon);
    DeleteObject(mask);
    return result;
}

bool drawRing(HWND window, POINT point, int size, Badge badge, BYTE alpha) {
    Surface drawing(size);
    if (!drawing.bitmap) return false;
    drawing.draw(badge, true, alpha);
    SIZE dimensions{size, size};
    POINT source{};
    BLENDFUNCTION blend{AC_SRC_OVER, 0, 255, AC_SRC_ALPHA};
    return UpdateLayeredWindow(window, nullptr, &point, &dimensions, drawing.dc, &source, 0, &blend, ULW_ALPHA);
}

Taskbar findTaskbar() {
    Taskbar result;
    const auto window = FindWindowW(L"Shell_TrayWnd", nullptr);
    if (!window || !IsWindowVisible(window) || !GetWindowRect(window, &result.bounds)) return {};
    const auto monitor = MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST);
    MONITORINFO info{};
    info.cbSize = sizeof(info);
    RECT intersection{};
    if (!GetMonitorInfoW(monitor, &info) || !IntersectRect(&intersection, &info.rcMonitor, &result.bounds)) return {};
    const LONG width = result.bounds.right - result.bounds.left, height = result.bounds.bottom - result.bounds.top;
    result.vertical = height > width;
    const LONG thickness = result.vertical ? intersection.right - intersection.left : intersection.bottom - intersection.top;
    if (thickness < 12) return {};
    const auto tray = FindWindowExW(window, nullptr, L"TrayNotifyWnd", nullptr);
    if (!tray || !GetWindowRect(tray, &result.tray)) return {};
    RECT trayIntersection{};
    if (!IntersectRect(&trayIntersection, &result.bounds, &result.tray)) return {};
    if (!result.vertical) {
        TrayLeft extent{result.tray, result.tray.left};
        EnumChildWindows(tray, includeTrayChildren, reinterpret_cast<LPARAM>(&extent));
        result.tray.left = extent.left;
    }
    result.window = window;
    return result;
}

bool coveredByTaskbar(HWND ring, HWND taskbar) {
    for (HWND above = GetWindow(ring, GW_HWNDPREV); above; above = GetWindow(above, GW_HWNDPREV)) {
        if (above == taskbar) return true;
    }
    return false;
}

bool fullscreenOn(const RECT& taskbar) {
    const HWND front = GetForegroundWindow();
    if (!front || !IsWindowVisible(front) || IsIconic(front)) return false;
    const auto display = MonitorFromRect(&taskbar, MONITOR_DEFAULTTONEAREST);
    if (display != MonitorFromWindow(front, MONITOR_DEFAULTTONEAREST)) return false;
    wchar_t type[64]{};
    GetClassNameW(front, type, std::size(type));
    if (wcscmp(type, L"Progman") == 0 || wcscmp(type, L"WorkerW") == 0
        || wcscmp(type, L"Shell_TrayWnd") == 0 || wcscmp(type, L"PbtNativePanel") == 0) return false;
    MONITORINFO screen{};
    screen.cbSize = sizeof(screen);
    RECT bounds{};
    if (!GetMonitorInfoW(display, &screen) || !GetWindowRect(front, &bounds)) return false;
    const auto edge = screen.rcMonitor;
    return bounds.left <= edge.left + 2 && bounds.top <= edge.top + 2
        && bounds.right >= edge.right - 2 && bounds.bottom >= edge.bottom - 2;
}

} // namespace pbt
