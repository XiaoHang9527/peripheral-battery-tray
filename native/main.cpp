#include "devices.hpp"
#include "graphics.hpp"
#include "preferences.hpp"
#include "update.hpp"

#include <commctrl.h>
#include <dbt.h>
#include <wtypes.h>
#include <gdiplus.h>
#include <shellapi.h>
#include <uxtheme.h>
#include <windowsx.h>

#include <array>
#include <cmath>
#include <condition_variable>
#include <fstream>
#include <mutex>
#include <optional>
#include <set>
#include <string_view>
#include <thread>
#include <utility>

namespace pbt {
namespace {

constexpr auto mutexName = L"Local\\PeripheralBatteryTray";
constexpr auto hostClass = L"PbtNativeHost";
constexpr auto panelClass = L"PbtNativePanel";
constexpr auto ringClass = L"PbtNativeRing";
constexpr auto hoverClass = L"PbtNativeHover";
constexpr auto repositoryUrl = L"https://github.com/XiaoHang9527/peripheral-battery-tray";
constexpr UINT trayMessage = WM_APP + 1;
constexpr UINT sampleMessage = WM_APP + 2;
constexpr UINT updateMessage = WM_APP + 3;
constexpr UINT reopenMessage = WM_APP + 4;
constexpr UINT styleTimer = 1;
constexpr UINT deviceTimer = 2;
constexpr UINT trayPanelTimer = 3;
constexpr UINT hoverTimer = 4;
constexpr UINT ringPulseTimer = 5;
constexpr UINT updateCleanupTimer = 6;
constexpr UINT hoverDelay = 180;
constexpr UINT trayId = 1;
constexpr DWORD pollIdleMs = 15'000;
constexpr DWORD pollInputSettleMs = 400;

DWORD inputIdleMs() {
    LASTINPUTINFO input{sizeof(input), 0};
    return GetLastInputInfo(&input) ? GetTickCount() - input.dwTime : pollIdleMs;
}

bool canPollAutomatically() {
    const auto idleMs = inputIdleMs();
    if (idleMs < pollInputSettleMs || idleMs >= pollIdleMs) return false;
    POINT cursor{};
    if (!GetCursorPos(&cursor)) return false;
    const auto window = WindowFromPoint(cursor);
    const auto root = window ? GetAncestor(window, GA_ROOT) : nullptr;
    wchar_t name[128]{};
    if (window) GetClassNameW(root ? root : window, name, std::size(name));
    constexpr std::wstring_view passiveWindows[]{ringClass, L"Shell_TrayWnd", L"Shell_SecondaryTrayWnd",
        L"NotifyIconOverflowWindow", L"TopLevelWindowForOverflowXamlIsland"};
    return std::find(std::begin(passiveWindows), std::end(passiveWindows), std::wstring_view(name))
        == std::end(passiveWindows);
}

enum Control : int {
    overviewTab = 100, settingsTab, list, renameEdit, rename, toggleRing, refresh, more, startup,
    update, logs, quit, systemTheme, lightTheme, darkTheme, interval, threshold, retention,
    globalRing, save, cancel, installUpdate, quickRing, resetDefaults, advancedLogs,
    logSize, logCopies, logDays, settingsScroll, openRepository
};

struct NumberField { int id, low, high, y; const wchar_t* label; const wchar_t* unit; int Options::*value; };
constexpr NumberField numberFields[] = {
    {retention, 0, 120, 231, L"掉线保留时长", L"分钟", &Options::retention},
    {interval, 5, 300, 328, L"刷新间隔", L"秒", &Options::interval},
    {threshold, 5, 50, 362, L"低电量阈值", L"%", &Options::low},
    {logSize, 64, 10240, 453, L"单份日志大小", L"KB", &Options::logSize},
    {logCopies, 1, 9, 487, L"保留历史日志", L"份", &Options::logCopies},
    {logDays, 0, 365, 521, L"历史日志保留时长", L"天", &Options::logDays},
};
constexpr int spinnerOffset = 100;
constexpr int panelWidth = 368, settingsTop = 55;
constexpr int settingsDeviceTop = 180, settingsRowHeight = 36, deviceSwitchWidth = 52;
constexpr int renameHeight = 28, renameSpace = 35;
constexpr int overviewListTop = 53, overviewRowHeight = 60;
constexpr int overviewActionsHeight = 52, deviceMenuWidth = 34;
constexpr int overviewActionInset = 9;
constexpr int overviewOptionsHeight = 76;

struct Palette {
    COLORREF surface, card, selected, text, secondary, border, accent, ink;
};

Palette palette(bool dark) {
    if (dark) return {RGB(43, 43, 43), RGB(56, 56, 56), RGB(48, 59, 73), RGB(248, 248, 248),
                      RGB(184, 184, 184), RGB(71, 71, 71), RGB(140, 186, 255), RGB(20, 38, 59)};
    return {RGB(243, 245, 247), RGB(255, 255, 255), RGB(232, 240, 250), RGB(32, 37, 44),
            RGB(83, 96, 112), RGB(220, 225, 231), RGB(36, 93, 166), RGB(255, 255, 255)};
}

bool useDark(Theme theme) {
    return theme == Theme::dark || (theme == Theme::system && systemDark(false));
}

HFONT uiFont(int size, int weight = FW_NORMAL) {
    return CreateFontW(-size, 0, 0, 0, weight, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
                       OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Microsoft YaHei UI");
}

void fill(HDC dc, RECT rect, COLORREF value) {
    const auto brush = CreateSolidBrush(value);
    FillRect(dc, &rect, brush);
    DeleteObject(brush);
}

void text(HDC dc, RECT rect, const std::wstring& value, COLORREF color, UINT flags = DT_LEFT) {
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, color);
    DrawTextW(dc, value.c_str(), static_cast<int>(value.size()), &rect,
              flags | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS);
}

std::wstring description(const Device& device) {
    if (!device.present) return L"已断开 · 上次读数";
    if (device.bluetooth) {
        if (device.fresh) return L"蓝牙已连接";
        return device.displayLevel() ? L"蓝牙已连接 · 上次读数" : L"蓝牙已连接 · 暂无电量";
    }
    if (device.readingUnconfirmed) return device.displayLevel() ? L"电量待确认 · 上次读数" : L"电量待确认";
    if (!device.fresh) return device.updated.time_since_epoch().count() ? L"暂未响应 · 上次读数" : L"正在等待读数";
    if (device.reading.wired) return L"已接线 · 电量未知";
    if (device.reading.fault) return L"电池状态异常";
    if (device.reading.charging) return L"充电中";
    return L"电池供电";
}

std::wstring displayLevel(const Device& device) {
    const auto level = device.displayLevel();
    return level ? std::to_wstring(*level) + L"%" : L"--";
}

std::wstring clockText(std::chrono::system_clock::time_point time) {
    const auto seconds = std::chrono::system_clock::to_time_t(time);
    tm local{};
    if (localtime_s(&local, &seconds) != 0) return {};
    wchar_t value[16]{};
    wcsftime(value, std::size(value), L"%H:%M", &local);
    return value;
}

bool pulses(const Badge& badge) {
    return badge.low && !badge.charging && !badge.faded && !badge.disconnected;
}

struct HoverReading {
    std::wstring title, level, caption, status, updated;
    COLORREF color{};
    bool dark{};
    bool operator==(const HoverReading&) const = default;
};

class App {
public:
    App(HINSTANCE instance, std::filesystem::path directory)
        : instance_(instance), directory_(std::move(directory)), preferences_(directory_),
          options_(preferences_.load()), preview_(options_), workerOptions_(options_) {}
    ~App() {
        stopWorker();
        if (worker_.joinable()) worker_.join();
        if (updater_.joinable()) updater_.join();
        discardUpdate(staged_);
        if (trayIcon_) DestroyIcon(trayIcon_);
        if (font_) DeleteObject(font_);
        if (titleFont_) DeleteObject(titleFont_);
        if (editBackground_) DeleteObject(editBackground_);
    }
    bool start();
    bool handleDialogMessage(MSG& message);

private:
    static LRESULT CALLBACK hostProc(HWND, UINT, WPARAM, LPARAM);
    static LRESULT CALLBACK panelProc(HWND, UINT, WPARAM, LPARAM);
    static LRESULT CALLBACK ringProc(HWND, UINT, WPARAM, LPARAM);
    static LRESULT CALLBACK hoverProc(HWND, UINT, WPARAM, LPARAM);
    static LRESULT CALLBACK listViewportProc(HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR);
    static LRESULT CALLBACK deviceListProc(HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR);
    bool registerClass(const wchar_t* name, WNDPROC procedure);
    void work();
    void stopWorker();
    std::uint64_t requestRefresh(bool showStatus);
    void requestRescan();
    void recordInput(HRAWINPUT input);
    void acceptSample();
    void diagnose(const std::wstring& message) const;
    void publishTray();
    TOOLINFOW trayTool();
    void trackTrayTip(bool show);
    bool positionTrayTip();
    void trayMenu();
    bool deviceMenu(HWND listWindow, POINT screenPoint, bool fromButton);
    void notifyLow();
    void updateRings();
    void hideRings();
    void pulseRings();
    bool trackRing(HWND window);
    void dismissHoverCard();
    void updateHoverCard();
    void paintHoverCard(HWND window);
    void toggleTaskbarRings();
    void openPanel(bool toSettings, RECT anchor, const std::wstring& key = {});
    std::optional<RECT> trayIconBounds() const;
    RECT trayAnchor() const;
    void createControls();
    void placeControls(bool batchPositions = true);
    void positionPanel(bool show = false);
    void showOptions(bool show);
    void dismiss();
    void redraw();
    void refreshList();
    void openRename();
    void finishRename(bool keepName);
    void updateRingControls();
    void command(int id, int code);
    void paintPanel();
    void drawItem(const DRAWITEMSTRUCT& item);
    void drawSwitch(HDC dc, const RECT& box, const Palette& colors, bool on, bool disabled) const;
    void updateCheck();
    void downloadUpdate();
    void endUpdate();
    void showSettings();
    void fillSettings();
    void scrollSettings(int delta);
    void revealSetting(int y, int height);
    void revealSelectedDevice();
    int settingsDeviceHeight() const;
    int renameTop() const { return settingsDeviceTop + settingsDeviceHeight() + 7; }
    int settingsShift() const;
    int settingsFooterHeight() const { return status_.empty() ? 53 : 77; }
    int settingsContentBottom() const;
    int overviewFooter() const;
    int overviewNoticeHeight() const;
    int overviewDeviceHeight() const;
    void applySettings();
    bool dark() const;
    int px(int value) const { return MulDiv(value, dpi_, 96); }
    Device* selected();
    std::wstring name(const Device&, bool preview = false) const;
    bool ringVisible(const Device&, bool preview = false) const;
    Badge badge(const Device&, bool dark) const;

    HINSTANCE instance_;
    HWND host_{}, panel_{}, listViewport_{}, hoverCard_{}, hoverRing_{}, trayTip_{};
    HDEVNOTIFY hotplug_{};
    UINT taskbarCreated_{}, dpi_ = 96;
    UINT fontDpi_{};
    HFONT font_{}, titleFont_{};
    HBRUSH editBackground_{};
    HICON trayIcon_{};
    Badge trayBadge_{};
    int trayIconSize_ = 0;
    std::wstring trayTipText_ = L"外设电量";
    std::optional<RECT> trayTipAnchor_;
    bool trayAdded_ = false, settings_ = false, expanded_ = false, checking_ = false;
    bool keepOpen_ = false;
    bool pulsing_ = false;
    bool advanced_ = false;
    int panelHeight_ = 0, scroll_ = 0;
    int overviewListHeight_ = overviewRowHeight;
    int overviewNoticeHeight_ = 12;
    RECT anchor_{}, pendingTrayAnchor_{};
    RECT hoverBounds_{};
    UINT hoverDpi_ = 96;
    HoverReading hoverReading_;
    Clock::time_point openingUntil_{};
    std::map<int, HWND> controls_;
    struct Ring {
        HWND window{};
        Badge image{};
        HWND taskbar{};
        int size{};
        POINT point{};
    };
    std::map<std::wstring, Ring> rings_;
    std::map<std::wstring, bool> visible_, alerts_;
    std::map<std::wstring, std::wstring> names_;
    std::map<std::wstring, bool> pendingRings_;
    std::map<std::wstring, std::wstring> pendingNames_;
    std::wstring currentKey_, renameKey_, status_;
    std::filesystem::path directory_;
    Preferences preferences_;
    Options options_, preview_;
    bool autostart_ = false;
    std::vector<Device> devices_;
    std::thread worker_, updater_;
    std::atomic_bool stop_ = false;
    std::mutex lock_;
    std::condition_variable wake_;
    Options workerOptions_;
    bool rescanRequested_ = false;
    std::map<HANDLE, std::pair<std::wstring, InputActivity>> rawInputs_;
    std::uint64_t requestedRefresh_ = 0, sampleRefresh_ = 0;
    std::uint64_t completedRefresh_ = 0, panelRefresh_ = 0, hoverRefresh_ = 0;
    std::vector<Device> sample_;
    std::vector<std::wstring> sampleDiagnostics_;
    UpdateResult release_;
    UpdateResult available_;
    std::filesystem::path staged_;
    bool downloading_ = false;
};

template<class T>
T* context(HWND window, UINT message, LPARAM value) {
    if (message == WM_NCCREATE) {
        const auto create = reinterpret_cast<CREATESTRUCTW*>(value);
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(create->lpCreateParams));
    }
    return reinterpret_cast<T*>(GetWindowLongPtrW(window, GWLP_USERDATA));
}

bool App::registerClass(const wchar_t* name, WNDPROC procedure) {
    WNDCLASSEXW type{};
    type.cbSize = sizeof(type);
    type.hInstance = instance_;
    type.lpszClassName = name;
    type.lpfnWndProc = procedure;
    type.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    return RegisterClassExW(&type) != 0;
}

bool App::start() {
    INITCOMMONCONTROLSEX common{sizeof(common), ICC_WIN95_CLASSES};
    InitCommonControlsEx(&common);
    taskbarCreated_ = RegisterWindowMessageW(L"TaskbarCreated");
    if (!registerClass(hostClass, hostProc) || !registerClass(panelClass, panelProc)
        || !registerClass(ringClass, ringProc) || !registerClass(hoverClass, hoverProc)) return false;
    host_ = CreateWindowExW(WS_EX_TOOLWINDOW, hostClass, L"外设电量", WS_POPUP,
                           0, 0, 0, 0, nullptr, nullptr, instance_, this);
    if (!host_) return false;
    trayTip_ = CreateWindowExW(WS_EX_TOPMOST | WS_EX_NOACTIVATE, TOOLTIPS_CLASSW, nullptr,
        WS_POPUP | TTS_ALWAYSTIP | TTS_NOPREFIX, CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT,
        host_, nullptr, instance_, nullptr);
    if (trayTip_) {
        auto tool = trayTool();
        if (SendMessageW(trayTip_, TTM_ADDTOOLW, 0, reinterpret_cast<LPARAM>(&tool))) {
            SetWindowTheme(trayTip_, L"", L"");
            SendMessageW(trayTip_, TTM_SETMAXTIPWIDTH, 0, 400);
        } else {
            DestroyWindow(trayTip_);
            trayTip_ = nullptr;
        }
    }
    DEV_BROADCAST_DEVICEINTERFACE_W filter{};
    filter.dbcc_size = sizeof(filter);
    filter.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE;
    hotplug_ = RegisterDeviceNotificationW(host_, &filter, DEVICE_NOTIFY_WINDOW_HANDLE | DEVICE_NOTIFY_ALL_INTERFACE_CLASSES);
    const RAWINPUTDEVICE inputs[]{
        {1, 2, RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, host_},
        {1, 6, RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, host_},
    };
    if (!RegisterRawInputDevices(inputs, std::size(inputs), sizeof(RAWINPUTDEVICE))) {
        diagnose(L"无法监听外设活动，可使用手动刷新");
    }
    SetTimer(host_, styleTimer, 1000, nullptr);
    SetTimer(host_, updateCleanupTimer, 5000, nullptr);
    publishTray();
    diagnose(L"启动外设电量 " + std::wstring(version));
    worker_ = std::thread([this] { work(); });
    return true;
}

LRESULT CALLBACK App::hostProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* app = context<App>(window, message, lparam);
    if (!app) return DefWindowProcW(window, message, wparam, lparam);
    if (app->taskbarCreated_ && message == app->taskbarCreated_) {
        app->trackTrayTip(false);
        app->trayAdded_ = false;
        app->publishTray();
        app->updateRings();
        return 0;
    }
    switch (message) {
    case WM_NOTIFY: {
        const auto* notice = reinterpret_cast<const NMHDR*>(lparam);
        if (notice && notice->hwndFrom == app->trayTip_ && notice->code == TTN_SHOW) {
            return app->positionTrayTip();
        }
        break;
    }
    case trayMessage:
        switch (LOWORD(lparam)) {
        case NIN_POPUPOPEN:
            app->trackTrayTip(true);
            break;
        case NIN_POPUPCLOSE:
            app->trackTrayTip(false);
            break;
        case NIN_SELECT:
        case NIN_KEYSELECT:
        case WM_LBUTTONUP:
            app->trackTrayTip(false);
            app->pendingTrayAnchor_ = app->trayAnchor();
            // Explorer can still be dismissing its overflow window when it sends the click.
            SetTimer(window, trayPanelTimer, 60, nullptr);
            break;
        case WM_CONTEXTMENU:
        case WM_RBUTTONUP:
            app->trackTrayTip(false);
            app->trayMenu();
            break;
        }
        return 0;
    case sampleMessage:
        app->acceptSample();
        return 0;
    case updateMessage:
        app->endUpdate();
        return 0;
    case reopenMessage:
        if (!app->panel_ || !IsWindowVisible(app->panel_)) app->openPanel(false, app->trayAnchor());
        else SetForegroundWindow(app->panel_);
        return 0;
    case WM_DEVICECHANGE:
        if (wparam == DBT_DEVICEARRIVAL || wparam == DBT_DEVICEREMOVECOMPLETE || wparam == DBT_DEVNODES_CHANGED) {
            SetTimer(window, deviceTimer, 300, nullptr);
        }
        return TRUE;
    case WM_INPUT:
        app->recordInput(reinterpret_cast<HRAWINPUT>(lparam));
        break; // DefWindowProc releases foreground raw-input messages.
    case WM_INPUT_DEVICE_CHANGE:
        if (wparam == GIDC_REMOVAL) {
            std::lock_guard guard(app->lock_);
            app->rawInputs_.erase(reinterpret_cast<HANDLE>(lparam));
        }
        return 0;
    case WM_POWERBROADCAST:
        if (wparam == PBT_APMRESUMESUSPEND || wparam == PBT_APMRESUMEAUTOMATIC) {
            {
                std::lock_guard guard(app->lock_);
                app->rawInputs_.clear();
            }
            app->requestRescan();
        }
        return TRUE;
    case WM_DISPLAYCHANGE:
    case WM_THEMECHANGED:
    case WM_SETTINGCHANGE:
        app->trackTrayTip(false);
        if (app->panel_ && IsWindowVisible(app->panel_)) app->redraw();
        app->publishTray();
        app->updateRings();
        return 0;
    case WM_TIMER:
        if (wparam == deviceTimer) {
            KillTimer(window, deviceTimer);
            app->requestRescan();
        } else if (wparam == trayPanelTimer) {
            KillTimer(window, trayPanelTimer);
            app->openPanel(false, app->pendingTrayAnchor_);
            app->updateRings();
        } else if (wparam == hoverTimer) {
            KillTimer(window, hoverTimer);
            app->updateHoverCard();
        } else if (wparam == ringPulseTimer) {
            app->pulseRings();
        } else if (wparam == updateCleanupTimer) {
            KillTimer(window, updateCleanupTimer);
            cleanupUpdateStages();
        } else if (wparam == styleTimer) {
            if (!app->trayAdded_) app->publishTray();
            app->updateRings();
        }
        return 0;
    case WM_CLOSE:
        DestroyWindow(window);
        return 0;
    case WM_DESTROY: {
        app->stopWorker();
        if (app->hotplug_) UnregisterDeviceNotification(app->hotplug_);
        NOTIFYICONDATAW icon{};
        icon.cbSize = sizeof(icon);
        icon.hWnd = window;
        icon.uID = trayId;
        Shell_NotifyIconW(NIM_DELETE, &icon);
        for (auto& [key, ring] : app->rings_) if (IsWindow(ring.window)) DestroyWindow(ring.window);
        PostQuitMessage(0);
        return 0;
    }
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

LRESULT CALLBACK App::panelProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* app = context<App>(window, message, lparam);
    if (!app) return DefWindowProcW(window, message, wparam, lparam);
    switch (message) {
    case WM_PAINT:
        if (app->panel_) app->paintPanel();
        else break;
        return 0;
    case WM_ERASEBKGND:
        return 1;
    case WM_DRAWITEM:
        app->drawItem(*reinterpret_cast<DRAWITEMSTRUCT*>(lparam));
        return TRUE;
    case WM_CTLCOLOREDIT:
    case WM_CTLCOLORLISTBOX: {
        const auto dc = reinterpret_cast<HDC>(wparam);
        const auto colors = palette(app->dark());
        SetTextColor(dc, colors.text);
        SetBkColor(dc, colors.card);
        return reinterpret_cast<LRESULT>(app->editBackground_);
    }
    case WM_COMMAND:
        app->command(LOWORD(wparam), HIWORD(wparam));
        return 0;
    case WM_DPICHANGED:
        if (app->panel_ && HIWORD(wparam) != app->dpi_) {
            app->dpi_ = HIWORD(wparam);
            app->positionPanel();
            app->placeControls();
        }
        return 0;
    case WM_MOUSEWHEEL:
        if (app->settings_) app->scrollSettings(-GET_WHEEL_DELTA_WPARAM(wparam) * 42 / WHEEL_DELTA);
        return 0;
    case WM_VSCROLL:
        if (reinterpret_cast<HWND>(lparam) != GetDlgItem(window, settingsScroll)) break;
        switch (LOWORD(wparam)) {
        case SB_LINEUP: app->scrollSettings(-24); break;
        case SB_LINEDOWN: app->scrollSettings(24); break;
        case SB_PAGEUP: app->scrollSettings(-120); break;
        case SB_PAGEDOWN: app->scrollSettings(120); break;
        case SB_THUMBPOSITION:
        case SB_THUMBTRACK: {
            SCROLLINFO info{sizeof(info), SIF_TRACKPOS, 0, 0, 0, 0, 0};
            if (GetScrollInfo(reinterpret_cast<HWND>(lparam), SB_CTL, &info)) {
                app->scrollSettings(info.nTrackPos - app->scroll_);
            }
            break;
        }
        case SB_TOP: app->scrollSettings(-app->scroll_); break;
        case SB_BOTTOM: app->scrollSettings(app->settingsContentBottom()); break;
        }
        return 0;
    case WM_ACTIVATE:
        if (LOWORD(wparam) == WA_INACTIVE && !app->keepOpen_ && Clock::now() >= app->openingUntil_) app->dismiss();
        return 0;
    case WM_CLOSE:
        app->dismiss();
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

LRESULT CALLBACK App::ringProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* app = context<App>(window, message, lparam);
    if (!app) return DefWindowProcW(window, message, wparam, lparam);
    if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (message == WM_MOUSEMOVE && app->hoverRing_ != window
        && (!app->panel_ || !IsWindowVisible(app->panel_))) {
        if (app->trackRing(window) && !SetTimer(app->host_, hoverTimer, hoverDelay, nullptr)) {
            app->dismissHoverCard();
        }
    }
    if ((message == WM_MOUSELEAVE || message == WM_DESTROY) && app->hoverRing_ == window) {
        app->dismissHoverCard();
    }
    if (message == WM_LBUTTONUP) {
        const bool panelVisible = app->panel_ && IsWindowVisible(app->panel_);
        const auto request = app->requestRefresh(panelVisible && !app->settings_);
        if (!panelVisible && app->trackRing(window)) {
            KillTimer(app->host_, hoverTimer);
            app->hoverRefresh_ = request;
            app->updateHoverCard();
        }
        return 0;
    }
    if (message == WM_RBUTTONUP) {
        for (const auto& [key, ring] : app->rings_) {
            if (ring.window != window) continue;
            RECT anchor{};
            GetWindowRect(window, &anchor);
            app->openPanel(false, anchor, key);
            app->updateRings();
            return 0;
        }
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

LRESULT CALLBACK App::hoverProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* app = context<App>(window, message, lparam);
    if (!app) return DefWindowProcW(window, message, wparam, lparam);
    switch (message) {
    case WM_PAINT: app->paintHoverCard(window); return 0;
    case WM_ERASEBKGND: return 1;
    case WM_NCHITTEST: return HTTRANSPARENT;
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

void App::work() {
    Scanner scanner;
    std::uint64_t processed = 0;
    std::unique_lock guard(lock_);
    while (!stop_) {
        const bool rescan = std::exchange(rescanRequested_, false);
        const auto request = requestedRefresh_;
        const bool manual = request != processed;
        const auto now = Clock::now();
        InputActivities recent;
        for (const auto& [handle, input] : rawInputs_) {
            if (!input.first.empty() && input.second.recent(now)) recent.emplace(input);
        }
        if (manual || rescan || !recent.empty() || now >= scanner.nextWake(workerOptions_.retention)) {
            const auto options = workerOptions_;
            guard.unlock();
            auto result = scanner.sample(stop_, options.interval, options.retention, manual, rescan, recent, canPollAutomatically);
            auto diagnostics = takeHidDiagnostics();
            guard.lock();
            if (stop_) break;
            processed = request;
            if (manual || result != sample_ || !diagnostics.empty()) {
                sample_ = std::move(result);
                for (auto& message : diagnostics) sampleDiagnostics_.push_back(std::move(message));
                sampleRefresh_ = processed;
                PostMessageW(host_, sampleMessage, 0, 0);
            }
        }
        wake_.wait_for(guard, std::chrono::milliseconds(500), [&] {
            return stop_ || requestedRefresh_ != processed || rescanRequested_;
        });
    }
}

void App::stopWorker() {
    {
        std::lock_guard guard(lock_);
        stop_ = true;
    }
    wake_.notify_one();
}

std::uint64_t App::requestRefresh(bool showStatus) {
    std::uint64_t request;
    {
        std::lock_guard guard(lock_);
        workerOptions_ = options_;
        request = ++requestedRefresh_;
    }
    wake_.notify_one();
    if (showStatus) {
        panelRefresh_ = request;
        status_ = L"正在刷新设备…";
        redraw();
    }
    return request;
}

void App::requestRescan() {
    {
        std::lock_guard guard(lock_);
        workerOptions_ = options_;
        rescanRequested_ = true;
    }
    wake_.notify_one();
}

void App::recordInput(HRAWINPUT input) {
    RAWINPUTHEADER header{};
    UINT size = sizeof(header);
    if (GetRawInputData(input, RID_HEADER, &header, &size, sizeof(header)) != sizeof(header)
        || !header.hDevice || (header.dwType != RIM_TYPEMOUSE && header.dwType != RIM_TYPEKEYBOARD)) return;
    std::lock_guard guard(lock_);
    auto [it, added] = rawInputs_.try_emplace(header.hDevice);
    auto& [path, activity] = it->second;
    if (added) {
        UINT length = 0;
        if (GetRawInputDeviceInfoW(header.hDevice, RIDI_DEVICENAME, nullptr, &length) == 0 && length > 1) {
            path.resize(length);
            if (GetRawInputDeviceInfoW(header.hDevice, RIDI_DEVICENAME, path.data(), &length) == UINT(-1)) path.clear();
            else {
                path.resize(wcslen(path.c_str()));
                CharUpperBuffW(path.data(), static_cast<DWORD>(path.size()));
            }
        }
    }
    if (!path.empty()) activity.observe(Clock::now());
}

void App::acceptSample() {
    const bool initial = devices_.empty();
    std::vector<std::wstring> diagnostics;
    {
        std::lock_guard guard(lock_);
        devices_ = sample_;
        diagnostics.swap(sampleDiagnostics_);
        completedRefresh_ = sampleRefresh_;
    }
    for (const auto& message : diagnostics) diagnose(message);
    if (initial && !devices_.empty()) diagnose(L"已识别 " + std::to_wstring(devices_.size()) + L" 个设备");
    for (const auto& device : devices_) {
        if (!names_.contains(device.key)) names_[device.key] = preferences_.alias(device.key);
        if (!visible_.contains(device.key)) visible_[device.key] = preferences_.ring(device.key);
    }
    std::erase_if(names_, [&](const auto& item) {
        return std::none_of(devices_.begin(), devices_.end(), [&](const Device& d) { return d.key == item.first; });
    });
    std::erase_if(visible_, [&](const auto& item) { return !names_.contains(item.first); });
    std::erase_if(alerts_, [&](const auto& item) { return !names_.contains(item.first); });
    if (!renameKey_.empty() && !names_.contains(renameKey_)) renameKey_.clear();
    if (status_ == L"正在刷新设备…" && completedRefresh_ >= panelRefresh_) {
        if (devices_.empty()) status_ = L"未检测到受支持的设备";
        else if (std::any_of(devices_.begin(), devices_.end(), [](const Device& device) { return device.fresh; })) {
            status_ = L"已刷新设备";
        } else if (std::any_of(devices_.begin(), devices_.end(), [](const Device& device) { return device.readingUnconfirmed; })) {
            status_ = L"电量待确认 · 可再次刷新";
        } else status_ = L"刷新未收到设备应答";
    }
    updateRings();
    publishTray();
    notifyLow();
    if (panel_ && IsWindowVisible(panel_)) {
        positionPanel();
        placeControls();
        refreshList();
    }
}

void App::diagnose(const std::wstring& message) const {
    if (directory_.empty()) return;
    const auto path = directory_ / L"tray.log";
    std::error_code error;
    const auto backup = [&](int index) {
        return directory_ / (index == 1 ? L"tray.previous.log" : L"tray.previous." + std::to_wstring(index) + L".log");
    };
    using FileClock = std::filesystem::file_time_type::clock;
    const auto cutoff = FileClock::now() - std::chrono::days(options_.logDays);
    for (int index = 1; index <= 9; ++index) {
        const auto copy = backup(index);
        const auto modified = std::filesystem::last_write_time(copy, error);
        if (!error && (index > options_.logCopies || (options_.logDays && modified < cutoff))) {
            std::filesystem::remove(copy, error);
        }
    }
    const auto size = std::filesystem::file_size(path, error);
    if (!error && size >= static_cast<std::uintmax_t>(options_.logSize) * 1024) {
        for (int index = options_.logCopies; index > 1; --index) {
            MoveFileExW(backup(index - 1).c_str(), backup(index).c_str(), MOVEFILE_REPLACE_EXISTING);
        }
        MoveFileExW(path.c_str(), backup(1).c_str(), MOVEFILE_REPLACE_EXISTING);
    }
    std::ofstream file(path, std::ios::app | std::ios::binary);
    file << toUtf8(clockText(std::chrono::system_clock::now()) + L" " + message) << "\r\n";
}

std::wstring App::name(const Device& device, bool preview) const {
    if (preview) {
        const auto pending = pendingNames_.find(device.key);
        if (pending != pendingNames_.end()) return pending->second.empty() ? device.name : pending->second;
    }
    const auto custom = names_.find(device.key);
    return custom != names_.end() && !custom->second.empty() ? custom->second : device.name;
}

bool App::ringVisible(const Device& device, bool preview) const {
    if (preview) {
        const auto pending = pendingRings_.find(device.key);
        if (pending != pendingRings_.end()) return pending->second;
    }
    const auto found = visible_.find(device.key);
    return found == visible_.end() || found->second;
}

Badge App::badge(const Device& device, bool darkTheme) const {
    const int level = device.displayLevel().value_or(-1);
    return {level, device.reading.charging, !device.fresh, !device.present, darkTheme,
            level >= 0 && level <= options_.low && !device.reading.fault};
}

std::optional<RECT> App::trayIconBounds() const {
    NOTIFYICONIDENTIFIER item{};
    item.cbSize = sizeof(item);
    item.hWnd = host_;
    item.uID = trayId;
    RECT result{};
    if (SUCCEEDED(Shell_NotifyIconGetRect(&item, &result)) && !IsRectEmpty(&result)
        && MonitorFromRect(&result, MONITOR_DEFAULTTONULL)) return result;
    return std::nullopt;
}

RECT App::trayAnchor() const {
    if (const auto bounds = trayIconBounds()) return *bounds;
    POINT cursor{};
    GetCursorPos(&cursor);
    return {cursor.x, cursor.y, cursor.x + 1, cursor.y + 1};
}

TOOLINFOW App::trayTool() {
    TOOLINFOW tool{};
    tool.cbSize = TTTOOLINFOW_V2_SIZE;
    tool.uFlags = TTF_TRACK | TTF_ABSOLUTE | TTF_TRANSPARENT;
    tool.hwnd = host_;
    tool.uId = trayId;
    tool.lpszText = trayTipText_.data();
    return tool;
}

void App::trackTrayTip(bool show) {
    if (!trayTip_) return;
    trayTipAnchor_.reset();
    auto tool = trayTool();
    if (show) {
        trayTipAnchor_ = trayIconBounds();
        POINT cursor{};
        if (!trayTipAnchor_ && GetCursorPos(&cursor) && MonitorFromPoint(cursor, MONITOR_DEFAULTTONULL)) {
            trayTipAnchor_ = RECT{cursor.x, cursor.y, cursor.x + 1, cursor.y + 1};
        }
        show = trayTipAnchor_.has_value();
        if (show) {
            const auto colors = palette(useDark(options_.theme));
            SendMessageW(trayTip_, TTM_SETTIPBKCOLOR, colors.card, 0);
            SendMessageW(trayTip_, TTM_SETTIPTEXTCOLOR, colors.text, 0);
            SendMessageW(trayTip_, TTM_TRACKPOSITION, 0, MAKELPARAM(trayTipAnchor_->left, trayTipAnchor_->top));
        }
    }
    SendMessageW(trayTip_, TTM_TRACKACTIVATE, show, reinterpret_cast<LPARAM>(&tool));
}

bool App::positionTrayTip() {
    // The tooltip has its measured size when the control sends TTN_SHOW.
    RECT current{};
    if (!trayTipAnchor_ || !GetWindowRect(trayTip_, &current) || IsRectEmpty(&current)) return false;
    RECT icon = *trayTipAnchor_;
    const POINT anchor{icon.right - 1, icon.top};
    const SIZE size{current.right - current.left, current.bottom - current.top};
    RECT popup{};
    return CalculatePopupWindowPosition(&anchor, &size,
               TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_VERTICAL | TPM_WORKAREA, &icon, &popup)
        && SetWindowPos(trayTip_, nullptr, popup.left, popup.top, 0, 0, SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOZORDER);
}

void App::publishTray() {
    const bool darkTheme = systemDark(true);
    const Device* indicated = nullptr;
    for (const auto& device : devices_) {
        if (!device.present || device.reading.wired || !device.reading.level) continue;
        if (!indicated || std::tuple(!device.fresh, device.reading.charging, device.reading.fault, *device.reading.level)
                          < std::tuple(!indicated->fresh, indicated->reading.charging, indicated->reading.fault,
                                       *indicated->reading.level)) indicated = &device;
    }
    Badge image;
    image.dark = darkTheme;
    if (indicated) image = badge(*indicated, darkTheme);
    const auto taskbar = FindWindowW(L"Shell_TrayWnd", nullptr);
    const auto trayDpi = GetDpiForWindow(taskbar ? taskbar : host_);
    const int iconSize = std::max(16, GetSystemMetricsForDpi(SM_CXSMICON, trayDpi ? trayDpi : 96));
    if (!trayIcon_ || image != trayBadge_ || trayIconSize_ != iconSize) {
        if (const auto icon = batteryIcon(image, iconSize)) {
            if (trayIcon_) DestroyIcon(trayIcon_);
            trayIcon_ = icon;
            trayBadge_ = image;
            trayIconSize_ = iconSize;
        }
    }
    NOTIFYICONDATAW item{};
    item.cbSize = sizeof(item);
    item.hWnd = host_;
    item.uID = trayId;
    item.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE;
    if (!trayTip_) item.uFlags |= NIF_SHOWTIP;
    item.uCallbackMessage = trayMessage;
    item.hIcon = trayIcon_ ? trayIcon_ : LoadIconW(nullptr, IDI_APPLICATION);
    std::wstring label = L"外设电量";
    const auto appendDevice = [&](const Device& device) {
        const auto status = L"  " + displayLevel(device);
        auto deviceName = name(device);
        // Leave room for other devices and keep the percentage intact even with a long alias.
        if (deviceName.size() > 32) {
            const auto last = deviceName[30];
            deviceName.resize(last >= 0xd800 && last <= 0xdbff ? 30 : 31);
            deviceName += L"…";
        }
        if (label.size() + 1 + deviceName.size() + status.size() < std::size(item.szTip)) {
            label += L"\n" + deviceName + status;
        }
    };
    if (indicated) appendDevice(*indicated);
    for (const auto& device : devices_) {
        if (&device != indicated && device.present) appendDevice(device);
    }
    if (label == L"外设电量") label += L"\n未检测到受支持的设备";
    wcsncpy(item.szTip, label.c_str(), std::size(item.szTip) - 1);
    if (trayTip_ && label != trayTipText_) {
        trayTipText_ = std::move(label);
        auto tool = trayTool();
        // Refresh the open tooltip instead of waiting for Explorer to reopen it.
        SendMessageW(trayTip_, TTM_UPDATETIPTEXTW, 0, reinterpret_cast<LPARAM>(&tool));
        SendMessageW(trayTip_, TTM_UPDATE, 0, 0);
        if (IsWindowVisible(trayTip_)) positionTrayTip();
    }
    if (trayAdded_ && Shell_NotifyIconW(NIM_MODIFY, &item)) return;
    trayAdded_ = Shell_NotifyIconW(NIM_ADD, &item);
    if (trayAdded_) {
        item.uVersion = NOTIFYICON_VERSION_4;
        Shell_NotifyIconW(NIM_SETVERSION, &item);
    }
}

void App::notifyLow() {
    if (!trayAdded_) return;
    for (const auto& device : devices_) {
        const auto& reading = device.reading;
        if (!device.fresh || !device.present || !reading.level) continue;
        bool& alerted = alerts_[device.key];
        if (*reading.level > options_.low + 5) alerted = false;
        if (alerted || *reading.level > options_.low || reading.wired || reading.charging || reading.fault) continue;
        NOTIFYICONDATAW data{};
        data.cbSize = sizeof(data);
        data.hWnd = host_;
        data.uID = trayId;
        data.uFlags = NIF_INFO;
        data.dwInfoFlags = NIIF_WARNING | NIIF_RESPECT_QUIET_TIME;
        const auto label = name(device) + L" 剩余 " + displayLevel(device);
        wcsncpy(data.szInfoTitle, L"外设电量偏低", std::size(data.szInfoTitle) - 1);
        wcsncpy(data.szInfo, label.c_str(), std::size(data.szInfo) - 1);
        alerted = Shell_NotifyIconW(NIM_MODIFY, &data);
    }
}

void App::trayMenu() {
    dismissHoverCard();
    const auto menu = CreatePopupMenu();
    if (!menu) return;
    AppendMenuW(menu, MF_STRING, overviewTab, L"设备概览");
    AppendMenuW(menu, MF_STRING, settingsTab, L"设置");
    AppendMenuW(menu, MF_STRING | (options_.rings ? MF_CHECKED : MF_UNCHECKED), quickRing, L"任务栏圆环");
    AppendMenuW(menu, MF_STRING, refresh, L"立即刷新");
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(menu, MF_STRING, quit, L"退出程序");
    POINT cursor{};
    GetCursorPos(&cursor);
    SetForegroundWindow(host_);
    const int choice = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                      cursor.x, cursor.y, 0, host_, nullptr);
    DestroyMenu(menu);
    PostMessageW(host_, WM_NULL, 0, 0);
    if (choice == overviewTab || choice == settingsTab) openPanel(choice == settingsTab, trayAnchor());
    else if (choice == quickRing) toggleTaskbarRings();
    else if (choice == refresh) requestRefresh(false);
    else if (choice == quit) PostMessageW(host_, WM_CLOSE, 0, 0);
}

LRESULT CALLBACK App::listViewportProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam, UINT_PTR subclass, DWORD_PTR) {
    if (message == WM_NCDESTROY) RemoveWindowSubclass(window, listViewportProc, subclass);
    if (message == WM_COMMAND || message == WM_DRAWITEM || message == WM_CTLCOLORLISTBOX) {
        return SendMessageW(GetParent(window), message, wparam, lparam);
    }
    return DefSubclassProc(window, message, wparam, lparam);
}

LRESULT CALLBACK App::deviceListProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam, UINT_PTR subclass, DWORD_PTR data) {
    auto* app = reinterpret_cast<App*>(data);
    if (message == WM_NCDESTROY) RemoveWindowSubclass(window, deviceListProc, subclass);
    if (message == WM_MOUSEWHEEL && app->settings_) return SendMessageW(app->panel_, message, wparam, lparam);
    if (message == WM_CONTEXTMENU) {
        if (app->deviceMenu(window, {GET_X_LPARAM(lparam), GET_Y_LPARAM(lparam)}, false)) return 0;
    } else if (message == WM_RBUTTONUP) {
        POINT point{GET_X_LPARAM(lparam), GET_Y_LPARAM(lparam)};
        ClientToScreen(window, &point);
        if (app->deviceMenu(window, point, false)) return 0;
    } else if (message == WM_KEYDOWN) {
        if (wparam == VK_APPS || (wparam == VK_F10 && GetKeyState(VK_SHIFT) < 0)) {
            if (app->deviceMenu(window, {-1, -1}, false)) return 0;
        } else if (wparam == VK_SPACE && app->settings_) {
            if (!(lparam & (1LL << 30))) app->command(toggleRing, BN_CLICKED);
            return 0;
        } else if (wparam == VK_F2 && app->settings_) {
            app->openRename();
            return 0;
        } else if (app->settings_ && (wparam == VK_UP || wparam == VK_DOWN || wparam == VK_HOME
                    || wparam == VK_END || wparam == VK_PRIOR || wparam == VK_NEXT)) {
            const auto result = DefSubclassProc(window, message, wparam, lparam);
            app->revealSelectedDevice();
            return result;
        }
    } else if (message == WM_LBUTTONUP) {
        const auto result = DefSubclassProc(window, message, wparam, lparam);
        POINT point{GET_X_LPARAM(lparam), GET_Y_LPARAM(lparam)};
        ClientToScreen(window, &point);
        app->deviceMenu(window, point, true);
        return result;
    }
    return DefSubclassProc(window, message, wparam, lparam);
}

bool App::deviceMenu(HWND listWindow, POINT screenPoint, bool fromButton) {
    const bool keyboard = !fromButton && screenPoint.x == -1 && screenPoint.y == -1;
    POINT point = screenPoint;
    LRESULT index = SendMessageW(listWindow, LB_GETCURSEL, 0, 0);
    if (!keyboard) {
        ScreenToClient(listWindow, &point);
        const auto hit = SendMessageW(listWindow, LB_ITEMFROMPOINT, 0, MAKELPARAM(point.x, point.y));
        if (HIWORD(hit)) return false;
        index = LOWORD(hit);
    }
    if (index < 0 || static_cast<std::size_t>(index) >= devices_.size()) return false;
    RECT row{};
    if (SendMessageW(listWindow, LB_GETITEMRECT, index, reinterpret_cast<LPARAM>(&row)) == LB_ERR) return false;
    if (!keyboard && !PtInRect(&row, point)) return false;
    const auto menuLeft = row.right - px(deviceMenuWidth);
    if (fromButton && point.x < menuLeft) {
        if (!settings_ || point.x < menuLeft - px(deviceSwitchWidth)) return false;
        currentKey_ = devices_[index].key;
        command(toggleRing, BN_CLICKED);
        return true;
    }
    if (keyboard || fromButton) {
        screenPoint = {menuLeft, row.bottom};
        ClientToScreen(listWindow, &screenPoint);
    }

    const auto menu = CreatePopupMenu();
    if (!menu) return false;
    const auto key = devices_[index].key;
    AppendMenuW(menu, MF_STRING, rename, L"重命名…");
    if (settings_ || options_.rings) AppendMenuW(menu, MF_STRING, toggleRing,
        ringVisible(devices_[index], settings_) ? L"隐藏此圆环" : L"显示此圆环");
    SendMessageW(listWindow, LB_SETCURSEL, index, 0);
    currentKey_ = key;
    SetFocus(listWindow);
    const bool wasKeptOpen = std::exchange(keepOpen_, true);
    const int choice = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                     screenPoint.x, screenPoint.y, 0, panel_, nullptr);
    keepOpen_ = wasKeptOpen;
    DestroyMenu(menu);
    PostMessageW(panel_, WM_NULL, 0, 0);
    if (choice && std::any_of(devices_.begin(), devices_.end(), [&](const Device& device) { return device.key == key; })) {
        currentKey_ = key;
        if (choice == rename) {
            if (!settings_) showSettings();
            currentKey_ = key;
            openRename();
        } else command(choice, BN_CLICKED);
    }
    return true;
}

void App::hideRings() {
    dismissHoverCard();
    if (std::exchange(pulsing_, false)) KillTimer(host_, ringPulseTimer);
    for (auto& [key, ring] : rings_) if (IsWindowVisible(ring.window)) ShowWindow(ring.window, SW_HIDE);
}

bool App::trackRing(HWND window) {
    if (hoverRing_ == window) return true;
    dismissHoverCard();
    TRACKMOUSEEVENT track{sizeof(track), TME_LEAVE, window, 0};
    if (!TrackMouseEvent(&track)) return false;
    hoverRing_ = window;
    return true;
}

void App::dismissHoverCard() {
    if (std::exchange(hoverRing_, nullptr)) KillTimer(host_, hoverTimer);
    hoverRefresh_ = 0;
    if (hoverCard_ && IsWindowVisible(hoverCard_)) ShowWindow(hoverCard_, SW_HIDE);
}

void App::updateHoverCard() {
    if (!hoverRing_) return;
    POINT cursor{};
    RECT ringBounds{};
    if (!IsWindowVisible(hoverRing_) || !GetCursorPos(&cursor) || WindowFromPoint(cursor) != hoverRing_
        || !GetWindowRect(hoverRing_, &ringBounds)) {
        dismissHoverCard();
        return;
    }
    const auto ring = std::find_if(rings_.begin(), rings_.end(), [&](const auto& item) {
        return item.second.window == hoverRing_;
    });
    if (ring == rings_.end()) return;
    const auto device = std::find_if(devices_.begin(), devices_.end(), [&](const Device& item) {
        return item.key == ring->first;
    });
    if (device == devices_.end()) return;

    const bool darkTheme = useDark(options_.theme);
    const auto reading = badge(*device, darkTheme);
    const bool historical = !device->present || !device->fresh;
    const bool known = reading.level >= 0;
    HoverReading card;
    card.title = name(*device);
    card.level = displayLevel(*device);
    card.caption = known ? L"上次电量" : L"电量未知";
    if (!device->present) card.status = L"已断开";
    else if (device->bluetooth) card.status = reading.low && device->fresh ? L"低电量" : L"蓝牙已连接";
    else if (device->readingUnconfirmed) card.status = L"电量待确认";
    else if (!device->fresh) card.status = L"未收到应答";
    else if (device->reading.wired) card.status = L"已接线";
    else if (device->reading.fault) card.status = L"电池状态异常";
    else if (device->reading.charging) card.status = L"充电中";
    else if (known) card.status = reading.low ? L"低电量" : L"电池供电";
    if (hoverRefresh_ > completedRefresh_) card.updated = L"正在刷新…";
    else if (device->bluetooth && !device->updated.time_since_epoch().count()) card.updated = L"尚未收到 Windows 电量";
    else if (device->bluetooth && historical) card.updated = L"上次读取于 " + clockText(device->updated);
    else if (hoverRefresh_ && historical && !device->readingUnconfirmed) card.updated = L"刷新未收到设备应答";
    else if (!device->updated.time_since_epoch().count()) card.updated = L"尚未读取电量";
    else if (historical) card.updated = L"上次应答于 " + clockText(device->updated);
    else {
        const auto age = std::chrono::system_clock::now() - device->updated;
        card.updated = age >= std::chrono::seconds(0) && age < std::chrono::seconds(10)
            ? L"刚刚更新" : L"更新于 " + clockText(device->updated);
    }
    card.dark = darkTheme;
    card.color = known && !historical && !device->reading.fault ? batteryColor(reading) : palette(darkTheme).secondary;

    MONITORINFO monitor{sizeof(monitor), {}, {}, 0};
    if (!GetMonitorInfoW(MonitorFromRect(&ringBounds, MONITOR_DEFAULTTONEAREST), &monitor)) {
        dismissHoverCard();
        return;
    }
    const UINT dpi = GetDpiForWindow(hoverRing_);
    const auto scale = [&](int value) { return MulDiv(value, dpi ? dpi : 96, 96); };
    const auto area = monitor.rcWork;
    const LONG width = std::min<LONG>(scale(180), area.right - area.left);
    const LONG height = std::min<LONG>(scale(94), area.bottom - area.top);
    const LONG gap = scale(6);
    const LONG x = std::clamp((ringBounds.left + ringBounds.right - width) / 2, area.left, area.right - width);
    const LONG above = ringBounds.top - height - gap;
    const LONG y = std::clamp(above >= area.top ? above : ringBounds.bottom + gap, area.top, area.bottom - height);
    const RECT bounds{x, y, x + width, y + height};

    const bool redraw = hoverReading_ != card || hoverDpi_ != dpi;
    hoverReading_ = std::move(card);
    hoverDpi_ = dpi ? dpi : 96;
    if (!hoverCard_) {
        hoverCard_ = CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST, hoverClass, L"外设电量",
            WS_POPUP, x, y, width, height, host_, nullptr, instance_, this);
        if (!hoverCard_) return;
    }
    if (!IsWindowVisible(hoverCard_) || !EqualRect(&hoverBounds_, &bounds)) {
        if (SetWindowPos(hoverCard_, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)) {
            hoverBounds_ = bounds;
        }
    }
    if (redraw) InvalidateRect(hoverCard_, nullptr, FALSE);
}

void App::paintHoverCard(HWND window) {
    PAINTSTRUCT paint{};
    const auto dc = BeginPaint(window, &paint);
    RECT client{};
    GetClientRect(window, &client);
    const auto& reading = hoverReading_;
    const auto colors = palette(reading.dark);
    const auto scale = [&](int value) { return MulDiv(value, hoverDpi_, 96); };
    fill(dc, client, colors.border);
    InflateRect(&client, -1, -1);
    fill(dc, client, colors.card);

    const auto titleFont = uiFont(scale(13), FW_SEMIBOLD);
    const auto levelFont = uiFont(scale(24), FW_BOLD);
    const auto detailFont = uiFont(scale(11));
    const int left = scale(11);
    const auto line = [&](int x, int y, int height, const std::wstring& label, COLORREF color) {
        text(dc, {x, scale(y), client.right - scale(9), scale(y + height)}, label, color);
    };
    const auto old = SelectObject(dc, titleFont);
    line(left, 6, 19, reading.title, colors.text);
    SelectObject(dc, levelFont);
    line(left, 24, 34, reading.level, reading.color);
    SIZE levelSize{};
    GetTextExtentPoint32W(dc, reading.level.c_str(), static_cast<int>(reading.level.size()), &levelSize);
    SelectObject(dc, detailFont);
    const int details = left + levelSize.cx + scale(9);
    const bool hasStatus = !reading.status.empty();
    line(details, hasStatus ? 27 : 34, 14, reading.caption, colors.secondary);
    if (hasStatus) line(details, 41, 14, reading.status, colors.secondary);
    line(left, 58, 15, reading.updated, colors.secondary);
    line(left, 75, 14, L"左键刷新 · 右键概览", colors.secondary);
    SelectObject(dc, old);
    DeleteObject(titleFont);
    DeleteObject(levelFont);
    DeleteObject(detailFont);
    EndPaint(window, &paint);
}

void App::toggleTaskbarRings() {
    const bool next = !options_.rings;
    if (preferences_.setRings(next)) {
        options_.rings = next;
        preview_.rings = next;
        status_ = next ? L"已开启任务栏圆环" : L"已关闭任务栏圆环";
        updateRings();
        if (panel_) updateRingControls();
    } else status_ = L"无法保存圆环设置";
    redraw();
}

void App::updateRings() {
    const auto bar = findTaskbar();
    if (!options_.rings || !bar.window || fullscreenOn(bar.bounds)) {
        hideRings();
        return;
    }
    const auto thickness = bar.vertical ? bar.bounds.right - bar.bounds.left : bar.bounds.bottom - bar.bounds.top;
    const int size = std::min<int>(thickness, ringWindowPixels);
    if (size < 18) {
        hideRings();
        return;
    }
    LONG edge = (bar.vertical ? bar.tray.top : bar.tray.left) - trayClearancePixels;
    const bool taskbarDark = systemDark(true);
    std::set<std::wstring> shown;
    for (const auto& device : devices_) {
        if (!visible_[device.key] || (!device.present && !device.reading.level)) continue;
        edge -= size;
        if (edge < (bar.vertical ? bar.bounds.top : bar.bounds.left) + size * 3) break;
        auto& ring = rings_[device.key];
        const bool existing = IsWindow(ring.window);
        if (!existing) {
            ring.window = CreateWindowExW(WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                ringClass, L"设备电量", WS_POPUP, 0, 0, size, size, host_, nullptr, instance_, this);
            ring.size = 0;
        }
        if (!ring.window) continue;
        if (ring.taskbar != bar.window || !existing) {
            SetWindowLongPtrW(ring.window, GWLP_HWNDPARENT, reinterpret_cast<LONG_PTR>(bar.window));
            ring.taskbar = bar.window;
        }
        POINT position = bar.vertical
            ? POINT{(bar.bounds.left + bar.bounds.right - size) / 2, edge}
            : POINT{edge, (bar.bounds.top + bar.bounds.bottom - size) / 2};
        const Badge image = badge(device, taskbarDark);
        if (ring.size != size || ring.image != image) {
            if (!drawRing(ring.window, position, size, image)) continue;
            ring.image = image;
            ring.size = size;
        }
        const bool moved = ring.point.x != position.x || ring.point.y != position.y;
        if (!IsWindowVisible(ring.window) || moved || coveredByTaskbar(ring.window, bar.window)) {
            if (SetWindowPos(ring.window, HWND_TOPMOST, position.x, position.y, 0, 0,
                             SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW)) {
                ring.point = position;
            }
        }
        shown.insert(device.key);
        edge -= ringGapPixels;
    }
    for (auto it = rings_.begin(); it != rings_.end();) {
        if (shown.contains(it->first)) {
            ++it;
            continue;
        }
        if (IsWindow(it->second.window)) DestroyWindow(it->second.window);
        it = rings_.erase(it);
    }
    const bool shouldPulse = std::any_of(rings_.begin(), rings_.end(), [](const auto& entry) {
        const auto& ring = entry.second;
        return ring.size && IsWindowVisible(ring.window) && pulses(ring.image);
    });
    if (shouldPulse && !pulsing_) pulsing_ = SetTimer(host_, ringPulseTimer, 150, nullptr) != 0;
    else if (!shouldPulse && std::exchange(pulsing_, false)) KillTimer(host_, ringPulseTimer);
    if (hoverCard_ && IsWindowVisible(hoverCard_)) updateHoverCard();
}

void App::pulseRings() {
    constexpr double cycle = 2400.0;
    constexpr double tau = 6.283185307179586;
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
    const double phase = std::fmod(static_cast<double>(elapsed), cycle) / cycle;
    const auto alpha = static_cast<BYTE>(std::lround(255 * (.55 + .45 * (1 - std::cos(phase * tau)) / 2)));
    for (const auto& [key, ring] : rings_) {
        if (ring.size && IsWindowVisible(ring.window) && pulses(ring.image)) {
            drawRing(ring.window, ring.point, ring.size, ring.image, alpha);
        }
    }
}

bool App::dark() const {
    return useDark(preview_.theme);
}

void App::createControls() {
    const auto control = [&](int id, const wchar_t* type, const wchar_t* caption, DWORD style = 0) {
        controls_[id] = CreateWindowExW(0, type, caption, WS_CHILD | style, 0, 0, 0, 0,
            id == list ? listViewport_ : panel_,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)), instance_, nullptr);
    };
    const auto button = [&](int id, const wchar_t* caption) {
        control(id, L"BUTTON", caption, BS_OWNERDRAW | WS_TABSTOP);
    };
    button(refresh, L"立即刷新");
    button(systemTheme, L"跟随 Windows");
    button(lightTheme, L"浅色");
    button(darkTheme, L"深色");
    button(globalRing, L"全部圆环");
    listViewport_ = CreateWindowExW(WS_EX_CONTROLPARENT, L"STATIC", nullptr, WS_CHILD | WS_CLIPCHILDREN,
                                    0, 0, 0, 0, panel_, nullptr, instance_, nullptr);
    SetWindowSubclass(listViewport_, listViewportProc, 1, 0);
    control(list, L"LISTBOX", nullptr,
            LBS_NOTIFY | LBS_OWNERDRAWFIXED | LBS_HASSTRINGS | LBS_NOINTEGRALHEIGHT | WS_VSCROLL | WS_TABSTOP | WS_VISIBLE);
    SetWindowSubclass(controls_[list], deviceListProc, 1, reinterpret_cast<DWORD_PTR>(this));
    control(renameEdit, L"EDIT", nullptr, ES_AUTOHSCROLL | WS_TABSTOP | WS_BORDER);
    SendMessageW(controls_[renameEdit], EM_SETLIMITTEXT, 64, 0);
    button(rename, L"确定名称");
    button(more, L"更多操作");
    button(quickRing, L"任务栏圆环");
    button(startup, L"开机自启");
    button(logs, L"打开日志");
    button(openRepository, L"GitHub 仓库");
    button(update, L"检查更新");
    button(settingsTab, L"设置…");
    button(quit, L"退出程序");
    button(installUpdate, L"下载更新");
    for (const auto field : numberFields) {
        if (field.id == logSize) button(advancedLogs, L"高级日志设置");
        control(field.id, L"EDIT", nullptr, ES_NUMBER | WS_TABSTOP | WS_BORDER);
        SendMessageW(controls_[field.id], EM_SETLIMITTEXT, 5, 0);
        control(field.id + spinnerOffset, UPDOWN_CLASSW, nullptr, UDS_ARROWKEYS | UDS_SETBUDDYINT | UDS_NOTHOUSANDS);
        SendMessageW(controls_[field.id + spinnerOffset], UDM_SETBUDDY, reinterpret_cast<WPARAM>(controls_[field.id]), 0);
        SendMessageW(controls_[field.id + spinnerOffset], UDM_SETRANGE32, field.low, field.high);
    }
    control(settingsScroll, L"SCROLLBAR", nullptr, SBS_VERT | WS_TABSTOP);
    button(resetDefaults, L"恢复默认");
    button(cancel, L"取消");
    button(save, L"保存");
}

int App::settingsDeviceHeight() const {
    return static_cast<int>(std::max<std::size_t>(1, devices_.size())) * settingsRowHeight;
}

int App::settingsShift() const {
    return settingsDeviceHeight() - settingsRowHeight + (renameKey_.empty() ? 0 : renameSpace);
}

int App::settingsContentBottom() const {
    return 447 + settingsShift() + (advanced_ ? 122 : 0);
}

int App::overviewFooter() const {
    return overviewListTop + overviewListHeight_ + overviewNoticeHeight_;
}

int App::overviewDeviceHeight() const {
    return expanded_ ? overviewListHeight_ - overviewOptionsHeight : overviewListHeight_;
}

int App::overviewNoticeHeight() const {
    const bool updateButton = !available_.downloadPath.empty() && !downloading_;
    return status_.empty() && !updateButton ? 12 : 38;
}

void App::placeControls(bool batchPositions) {
    if (controls_.empty()) return;
    if (fontDpi_ != dpi_) {
        const auto old = font_, oldTitle = titleFont_;
        font_ = uiFont(px(13));
        titleFont_ = uiFont(px(19), FW_SEMIBOLD);
        fontDpi_ = dpi_;
        for (const auto& [id, field] : controls_) {
            SendMessageW(field, WM_SETFONT, reinterpret_cast<WPARAM>(font_), FALSE);
        }
        if (old) DeleteObject(old);
        if (oldTitle) DeleteObject(oldTitle);
    }
    auto positions = batchPositions ? BeginDeferWindowPos(static_cast<int>(controls_.size())) : nullptr;
    const bool deferred = positions != nullptr;
    const auto placeWindow = [&](HWND field, int x, int y, int width, int height, bool show) {
        const bool visible = (GetWindowLongPtrW(field, GWL_STYLE) & WS_VISIBLE) != 0;
        if (!show && !visible) return;
        UINT flags = SWP_NOZORDER | SWP_NOACTIVATE;
        if (show && visible) {
            RECT bounds{};
            GetWindowRect(field, &bounds);
            MapWindowPoints(HWND_DESKTOP, panel_, reinterpret_cast<POINT*>(&bounds), 2);
            if (bounds.left == x && bounds.top == y && bounds.right - bounds.left == width
                && bounds.bottom - bounds.top == height) return;
        }
        if (show && !visible) flags |= SWP_SHOWWINDOW;
        if (!show) flags |= SWP_HIDEWINDOW | SWP_NOMOVE | SWP_NOSIZE;
        if (!deferred) SetWindowPos(field, nullptr, x, y, width, height, flags);
        else if (positions) positions = DeferWindowPos(positions, field, nullptr, x, y, width, height, flags);
    };
    const auto place = [&](int id, int x, int y, int width, int height, bool show) {
        placeWindow(controls_.at(id), px(x), px(y), px(width), px(height), show);
    };
    const int footer = settings_ ? panelHeight_ - settingsFooterHeight() : overviewFooter();
    const int viewport = footer - settingsTop;
    const int maxScroll = std::max(0, settingsContentBottom() - footer);
    scroll_ = std::clamp(scroll_, 0, maxScroll);
    const auto form = [&](int id, int x, int y, int w, int h, bool show = true) {
        const int top = y - scroll_;
        place(id, x, top, w, h, settings_ && show && top >= settingsTop && top + h <= footer);
    };

    const bool renaming = !renameKey_.empty();
    const int listTop = px(settings_ ? settingsDeviceTop - scroll_ : overviewListTop);
    const int rowHeight = px(settings_ ? settingsRowHeight : overviewRowHeight);
    const int listHeight = settings_ ? rowHeight * (settingsDeviceHeight() / settingsRowHeight) : px(overviewDeviceHeight());
    const int clipTop = settings_ ? std::max(listTop, px(settingsTop)) : listTop;
    const int clipBottom = settings_ ? std::min(listTop + listHeight, px(footer)) : listTop + listHeight;
    // Keep every settings row in the list and clip it to the page's scroll area.
    placeWindow(listViewport_, px(16), clipTop, px(336), clipBottom - clipTop, clipBottom > clipTop);
    form(renameEdit, 16, renameTop(), 254, renameHeight, renaming);
    form(rename, 278, renameTop(), 74, renameHeight, renaming);
    place(refresh, 254, 12, 98, 33, !settings_);
    place(more, 16, footer + overviewActionInset, 92, 33, !settings_);
    place(settingsTab, 200, footer + overviewActionInset, 64, 33, !settings_);
    place(quit, 272, footer + overviewActionInset, 80, 33, !settings_);
    const int optionsTop = overviewListTop + overviewDeviceHeight();
    place(quickRing, 16, optionsTop + 7, 164, 32, !settings_ && expanded_);
    place(startup, 188, optionsTop + 7, 164, 32, !settings_ && expanded_);
    place(logs, 16, optionsTop + 44, 104, 26, !settings_ && expanded_);
    place(openRepository, 132, optionsTop + 44, 104, 26, !settings_ && expanded_);
    place(update, 248, optionsTop + 44, 104, 26, !settings_ && expanded_);
    place(installUpdate, 265, footer - 34, 87, 25, !settings_ && !available_.downloadPath.empty() && !downloading_);

    form(systemTheme, 16, 82, 108, 43);
    form(lightTheme, 130, 82, 108, 43);
    form(darkTheme, 244, 82, 108, 43);
    form(globalRing, 352 - deviceMenuWidth - 128, 143, 128, 31);
    const int shift = settingsShift();
    for (const auto field : numberFields) {
        const bool show = field.id < logSize || advanced_;
        form(field.id, 242, field.y + shift, 58, 26, show);
        form(field.id + spinnerOffset, 300, field.y + shift, 16, 26, show);
    }
    form(advancedLogs, 16, 410 + shift, 122, 32);

    if (settings_ && maxScroll > 0 && viewport > 0) {
        const SCROLLINFO info{sizeof(info), SIF_RANGE | SIF_PAGE | SIF_POS, 0,
            settingsContentBottom() - settingsTop - 1, static_cast<UINT>(viewport), scroll_, 0};
        SetScrollInfo(controls_[settingsScroll], SB_CTL, &info, TRUE);
    }
    place(settingsScroll, 354, settingsTop, 13, std::max(0, viewport), settings_ && maxScroll > 0);
    const int buttons = footer + settingsFooterHeight() - 45;
    place(resetDefaults, 16, buttons, 84, 33, settings_);
    place(cancel, 208, buttons, 67, 33, settings_);
    place(save, 283, buttons, 69, 33, settings_);

    if (deferred && (!positions || !EndDeferWindowPos(positions))) {
        placeControls(false);
        return;
    }
    const auto box = controls_[list];
    if (SendMessageW(box, LB_GETITEMHEIGHT, 0, 0) != rowHeight) {
        SendMessageW(box, LB_SETITEMHEIGHT, 0, rowHeight);
    }
    RECT bounds{};
    GetWindowRect(box, &bounds);
    MapWindowPoints(HWND_DESKTOP, listViewport_, reinterpret_cast<POINT*>(&bounds), 2);
    if (bounds.top != listTop - clipTop || bounds.right - bounds.left != px(336) || bounds.bottom - bounds.top != listHeight) {
        SetWindowPos(box, nullptr, 0, listTop - clipTop, px(336), listHeight, SWP_NOZORDER | SWP_NOACTIVATE);
    }
    if (settings_ && SendMessageW(box, LB_GETTOPINDEX, 0, 0) > 0) SendMessageW(box, LB_SETTOPINDEX, 0, 0);
    SetWindowTextW(controls_[more], expanded_ ? L"收起操作" : L"更多操作");
    updateRingControls();
}

void App::scrollSettings(int delta) {
    const int maxScroll = std::max(0, settingsContentBottom() - (panelHeight_ - settingsFooterHeight()));
    const int next = std::clamp(scroll_ + delta, 0, maxScroll);
    if (next == scroll_) return;
    scroll_ = next;
    placeControls();
    redraw();
}

void App::revealSetting(int y, int height) {
    const int footer = panelHeight_ - settingsFooterHeight();
    if (y - scroll_ < settingsTop) scrollSettings(y - scroll_ - settingsTop);
    else if (y + height - scroll_ > footer) scrollSettings(y + height - scroll_ - footer);
}

void App::revealSelectedDevice() {
    const auto index = SendMessageW(controls_[list], LB_GETCURSEL, 0, 0);
    if (index != LB_ERR) revealSetting(settingsDeviceTop + static_cast<int>(index) * settingsRowHeight, settingsRowHeight);
}

void App::positionPanel(bool show) {
    if (!panel_) return;
    MONITORINFO monitor{};
    monitor.cbSize = sizeof(monitor);
    if (!GetMonitorInfoW(MonitorFromRect(&anchor_, MONITOR_DEFAULTTONEAREST), &monitor)) return;
    const auto area = monitor.rcWork;
    const int width = std::min<int>(px(panelWidth), area.right - area.left);
    const int inset = px(7);
    const LONG wantX = anchor_.right - width;
    const int availableHeight = MulDiv(area.bottom - area.top - px(4), 96, dpi_);
    if (settings_) {
        panelHeight_ = std::min(settingsContentBottom() + settingsFooterHeight(), availableHeight);
    } else {
        overviewNoticeHeight_ = overviewNoticeHeight();
        const int fixedHeight = overviewListTop + overviewNoticeHeight_ + overviewActionsHeight;
        const int rows = static_cast<int>(std::clamp<std::size_t>(devices_.size() + 1, 2, 4));
        // The reserved row becomes the inline controls, keeping the outer window and footer fixed.
        overviewListHeight_ = std::clamp(availableHeight - fixedHeight, overviewOptionsHeight + 1,
            (rows - 1) * overviewRowHeight + overviewOptionsHeight);
        panelHeight_ = overviewFooter() + overviewActionsHeight;
    }
    const int height = px(panelHeight_);
    const LONG wantY = anchor_.top - height - inset >= area.top ? anchor_.top - height - inset : anchor_.bottom + inset;
    SetWindowPos(panel_, HWND_TOPMOST, std::clamp(wantX, area.left, area.right - width),
        std::clamp(wantY, area.top, std::max(area.top, area.bottom - height)), width, height,
        SWP_NOACTIVATE | (show ? SWP_SHOWWINDOW : 0));
}

void App::showOptions(bool show) {
    if (settings_ || expanded_ == show) return;
    if (!show) {
        const auto focus = GetFocus();
        if (focus == controls_[quickRing] || focus == controls_[startup] || focus == controls_[logs]
            || focus == controls_[openRepository] || focus == controls_[update]) {
            SetFocus(controls_[more]);
        }
    }
    expanded_ = show;
    placeControls();
    const int bottom = overviewListTop + overviewListHeight_;
    const RECT section{0, px(bottom - overviewOptionsHeight), px(panelWidth), px(overviewFooter())};
    RedrawWindow(panel_, &section, nullptr, RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN);
}

void App::dismiss() {
    expanded_ = false;
    if (panel_ && IsWindowVisible(panel_)) ShowWindow(panel_, SW_HIDE);
}

bool App::handleDialogMessage(MSG& message) {
    if (!panel_ || !IsWindowVisible(panel_)) return false;
    if (message.message == WM_KEYDOWN && message.wParam == VK_ESCAPE
        && (message.hwnd == panel_ || IsChild(panel_, message.hwnd))) {
        if (settings_ && !renameKey_.empty()) finishRename(false);
        else if (expanded_) showOptions(false);
        else dismiss();
        return true;
    }
    if (message.message == WM_KEYDOWN && message.wParam == VK_RETURN && message.hwnd == controls_[renameEdit]) {
        finishRename(true);
        return true;
    }
    return IsDialogMessageW(panel_, &message);
}
void App::openPanel(bool toSettings, RECT anchor, const std::wstring& key) {
    dismissHoverCard();
    openingUntil_ = Clock::now() + std::chrono::milliseconds(300);
    const bool wasOpen = panel_ && IsWindowVisible(panel_);
    if (!wasOpen) {
        anchor_ = anchor;
        preview_ = options_;
        if (!options_.rings) expanded_ = true;
    }
    autostart_ = hasAutostart();
    if (!key.empty()) currentKey_ = key;
    if (!panel_) {
        panel_ = CreateWindowExW(WS_EX_TOOLWINDOW | WS_EX_CONTROLPARENT | WS_EX_TOPMOST | WS_EX_COMPOSITED, panelClass,
            L"外设电量", WS_POPUP | WS_BORDER | WS_CLIPCHILDREN, anchor_.left, anchor_.top, 1, 1, host_, nullptr, instance_, this);
        if (!panel_) return;
        dpi_ = GetDpiForWindow(panel_);
        if (!dpi_) dpi_ = 96;
        createControls();
    }
    if (toSettings) showSettings();
    else {
        settings_ = false;
        preview_ = options_;
        positionPanel();
        placeControls();
        refreshList();
    }
    redraw();
    positionPanel(true);
    if (SetForegroundWindow(panel_)) SetFocus(controls_[toSettings ? (renameKey_.empty() ? systemTheme : renameEdit) : list]);
}

void App::redraw() {
    if (!panel_) return;
    if (!settings_ && overviewNoticeHeight_ != overviewNoticeHeight()) {
        positionPanel();
        placeControls();
    }
    const auto old = editBackground_;
    editBackground_ = CreateSolidBrush(palette(dark()).card);
    RedrawWindow(panel_, nullptr, nullptr, RDW_INVALIDATE | RDW_ALLCHILDREN);
    if (old) DeleteObject(old);
}

Device* App::selected() {
    const auto it = std::find_if(devices_.begin(), devices_.end(), [&](const Device& d) { return d.key == currentKey_; });
    return it == devices_.end() ? nullptr : &*it;
}

void App::refreshList() {
    if (!panel_) return;
    const auto box = controls_[list];
    const auto top = SendMessageW(box, LB_GETTOPINDEX, 0, 0);
    SendMessageW(box, WM_SETREDRAW, FALSE, 0);
    SendMessageW(box, LB_RESETCONTENT, 0, 0);
    int selection = -1;
    for (std::size_t i = 0; i < devices_.size(); ++i) {
        auto& device = devices_[i];
        auto label = name(device, settings_) + L"  " + displayLevel(device) + L"  " + description(device);
        if (settings_) label += ringVisible(device, true) ? L"  圆环已开启" : L"  圆环已关闭";
        SendMessageW(box, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(label.c_str()));
        if (device.key == currentKey_) selection = static_cast<int>(i);
    }
    if (selection < 0 && !devices_.empty()) {
        selection = 0;
        currentKey_ = devices_.front().key;
    }
    if (selection < 0) currentKey_.clear();
    SendMessageW(box, LB_SETCURSEL, static_cast<WPARAM>(selection), 0);
    const int visibleRows = std::max(1, overviewDeviceHeight() / overviewRowHeight);
    if (!settings_ && top > 0 && selection >= top && selection < top + visibleRows) {
        SendMessageW(box, LB_SETTOPINDEX, top, 0);
    }
    SendMessageW(box, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(box, nullptr, TRUE);
    InvalidateRect(panel_, nullptr, FALSE);
}

void App::openRename() {
    const auto device = selected();
    if (!device) return;
    const auto key = device->key;
    if (renameKey_ != key) {
        if (!renameKey_.empty()) finishRename(true);
        currentKey_ = renameKey_ = key;
        SetWindowTextW(controls_[renameEdit], name(*device, true).c_str());
        positionPanel();
        placeControls();
        refreshList();
    }
    revealSetting(renameTop(), renameHeight);
    SetFocus(controls_[renameEdit]);
    SendMessageW(controls_[renameEdit], EM_SETSEL, 0, -1);
    redraw();
}

void App::finishRename(bool keepName) {
    if (renameKey_.empty()) return;
    if (keepName) {
        wchar_t value[65]{};
        GetWindowTextW(controls_[renameEdit], value, std::size(value));
        std::wstring custom(value);
        const auto begin = custom.find_first_not_of(L" \t");
        custom = begin == std::wstring::npos ? L"" : custom.substr(begin, custom.find_last_not_of(L" \t") - begin + 1);
        pendingNames_[renameKey_] = std::move(custom);
    }
    currentKey_ = std::exchange(renameKey_, {});
    positionPanel();
    placeControls();
    refreshList();
    revealSelectedDevice();
    SetFocus(controls_[list]);
    redraw();
}

void App::updateRingControls() {
    const auto setSwitch = [&](int id, std::wstring label, bool on) {
        label += on ? L"，已开启" : L"，已关闭";
        SetWindowTextW(controls_[id], label.c_str());
    };
    setSwitch(quickRing, L"任务栏圆环", options_.rings);
    setSwitch(globalRing, L"全部圆环", preview_.rings);
    setSwitch(startup, L"开机自启", autostart_);
}

void App::showSettings() {
    expanded_ = false;
    settings_ = true;
    preview_ = options_;
    pendingRings_.clear();
    pendingNames_.clear();
    if (!renameKey_.empty() && !names_.contains(renameKey_)) renameKey_.clear();
    if (!renameKey_.empty()) currentKey_ = renameKey_;
    scroll_ = 0;
    status_.clear();
    fillSettings();
    positionPanel();
    placeControls();
    refreshList();
    if (!renameKey_.empty()) revealSetting(renameTop(), renameHeight);
    SetFocus(controls_[renameKey_.empty() ? systemTheme : renameEdit]);
    redraw();
}

void App::fillSettings() {
    for (const auto field : numberFields) {
        SendMessageW(controls_[field.id + spinnerOffset], UDM_SETPOS32, 0, preview_.*(field.value));
    }
    SetWindowTextW(controls_[advancedLogs], advanced_ ? L"收起日志设置" : L"高级日志设置");
}

void App::applySettings() {
    finishRename(true);
    Options next = preview_;
    for (const auto field : numberFields) {
        wchar_t value[16]{};
        GetWindowTextW(controls_[field.id], value, std::size(value));
        wchar_t* end{};
        const long number = wcstol(value, &end, 10);
        if (end == value || *end || number < field.low || number > field.high) {
            if (field.id >= logSize) {
                advanced_ = true;
                SetWindowTextW(controls_[advancedLogs], L"收起日志设置");
            }
            status_ = std::wstring(field.label) + L"可设为 " + std::to_wstring(field.low) + L"–"
                + std::to_wstring(field.high) + L" " + field.unit;
            positionPanel();
            const int y = field.y + settingsShift();
            const int footer = panelHeight_ - settingsFooterHeight();
            if (y - scroll_ < settingsTop || y + 26 - scroll_ > footer) scroll_ = std::max(0, y - settingsTop - 6);
            placeControls();
            SetFocus(controls_[field.id]);
            SendMessageW(controls_[field.id], EM_SETSEL, 0, -1);
            redraw();
            return;
        }
        next.*(field.value) = static_cast<int>(number);
    }
    if (!preferences_.save(next, pendingRings_, pendingNames_)) {
        status_ = L"无法保存设置，请检查应用数据目录权限";
        positionPanel();
        placeControls();
    }
    else {
        options_ = preview_ = next;
        for (const auto& [key, visible] : pendingRings_) if (visible_.contains(key)) visible_[key] = visible;
        for (const auto& [key, alias] : pendingNames_) if (names_.contains(key)) names_[key] = alias;
        pendingRings_.clear();
        pendingNames_.clear();
        settings_ = false;
        status_ = L"设置已保存";
        positionPanel();
        placeControls();
        refreshList();
        SetFocus(controls_[list]);
        requestRescan();
        publishTray();
        updateRings();
        diagnose(L"设置已保存");
    }
    redraw();
}

void App::command(int id, int code) {
    if (id == list && code == LBN_SELCHANGE) {
        const auto index = SendMessageW(controls_[list], LB_GETCURSEL, 0, 0);
        if (index >= 0 && static_cast<std::size_t>(index) < devices_.size()) currentKey_ = devices_[index].key;
        return;
    }
    if (code != BN_CLICKED) return;
    switch (id) {
    case settingsTab: showSettings(); return;
    case overviewTab:
    case cancel:
        settings_ = false;
        preview_ = options_;
        pendingNames_.clear();
        pendingRings_.clear();
        renameKey_.clear();
        scroll_ = 0;
        status_.clear();
        positionPanel();
        placeControls();
        refreshList();
        SetFocus(controls_[list]);
        break;
    case systemTheme:
    case lightTheme:
    case darkTheme:
        preview_.theme = static_cast<Theme>(id - systemTheme);
        break;
    case globalRing:
        preview_.rings = !preview_.rings;
        updateRingControls();
        break;
    case resetDefaults:
        preview_ = Options{};
        for (const auto& device : devices_) pendingRings_[device.key] = true;
        fillSettings();
        updateRingControls();
        status_ = L"已载入默认值，保存后生效";
        positionPanel();
        placeControls();
        refreshList();
        break;
    case advancedLogs:
        advanced_ = !advanced_;
        SetWindowTextW(controls_[advancedLogs], advanced_ ? L"收起日志设置" : L"高级日志设置");
        positionPanel();
        placeControls();
        break;
    case quickRing:
        toggleTaskbarRings();
        return;
    case save: applySettings(); return;
    case refresh: requestRefresh(true); return;
    case more:
        showOptions(!expanded_);
        return;
    case rename: finishRename(true); return;
    case toggleRing:
        if (const auto device = selected()) {
            const bool next = !ringVisible(*device, settings_);
            if (settings_) {
                pendingRings_[device->key] = next;
                refreshList();
            } else if (preferences_.setRing(device->key, next)) {
                visible_[device->key] = next;
                status_ = next ? L"已显示此设备的圆环" : L"已隐藏，可通过设备菜单重新显示";
                updateRingControls();
                updateRings();
            } else status_ = L"无法保存圆环设置";
        }
        break;
    case startup:
        if (setAutostart(!autostart_)) {
            autostart_ = !autostart_;
            status_ = autostart_ ? L"已开启开机自启" : L"已关闭开机自启";
            updateRingControls();
        } else status_ = L"Windows 未允许修改开机自启";
        break;
    case logs:
        if (!directory_.empty()) ShellExecuteW(panel_, L"open", (directory_ / L"tray.log").c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        break;
    case openRepository: {
        constexpr auto error = L"无法打开 GitHub 仓库，请检查默认浏览器";
        const auto result = ShellExecuteW(panel_, L"open", repositoryUrl, nullptr, nullptr, SW_SHOWNORMAL);
        if (reinterpret_cast<INT_PTR>(result) <= 32) status_ = error;
        else if (status_ == error) status_.clear();
        break;
    }
    case update: updateCheck(); return;
    case installUpdate: downloadUpdate(); return;
    case quit: PostMessageW(host_, WM_CLOSE, 0, 0); return;
    }
    redraw();
}

void App::paintPanel() {
    PAINTSTRUCT paint{};
    HDC dc = BeginPaint(panel_, &paint);
    RECT client{};
    GetClientRect(panel_, &client);
    const auto colors = palette(dark());
    fill(dc, client, colors.surface);
    const auto rule = [&](int x, int y, int end) {
        fill(dc, {px(x), px(y), px(end), px(y) + 1}, colors.border);
    };
    const auto line = [&](int x, int y, int width, int height, const std::wstring& label, COLORREF color) {
        text(dc, {px(x), px(y), px(x + width), px(y + height)}, label, color);
    };
    SelectObject(dc, titleFont_);
    line(16, 13, 220, 31, settings_ ? L"设置" : L"外设电量", colors.text);
    SelectObject(dc, font_);
    if (settings_) {
        const int footer = panelHeight_ - settingsFooterHeight();
        const int saved = SaveDC(dc);
        IntersectClipRect(dc, 0, px(settingsTop), client.right, px(footer));
        const int shift = settingsShift();
        const auto section = uiFont(px(14), FW_SEMIBOLD);
        SelectObject(dc, section);
        line(16, 57 - scroll_, 200, 22, L"外观", colors.text);
        rule(16, 136 - scroll_, 352);
        line(16, 145 - scroll_, 196, 25, L"任务栏圆环", colors.text);
        rule(16, 289 + shift - scroll_, 352);
        line(16, 300 + shift - scroll_, 300, 23, L"刷新和提醒", colors.text);
        SelectObject(dc, font_);
        DeleteObject(section);
        for (const auto field : numberFields) {
            if (field.id >= logSize && !advanced_) continue;
            const int y = field.y + shift - scroll_;
            line(16, y, 211, 26, field.label, colors.text);
            line(323, y, 30, 26, field.unit, colors.secondary);
        }
        line(16, 260 + shift - scroll_, 336, 23, L"设为 0 分钟会在断开后立即隐藏圆环", colors.secondary);
        line(16, 389 + shift - scroll_, 336, 19, L"达到阈值时显示低电量提醒", colors.secondary);
        if (advanced_) line(16, 550 + shift - scroll_, 336, 17, L"设为 0 天时仅按份数保留历史日志", colors.secondary);
        RestoreDC(dc, saved);
        rule(0, footer, panelWidth);
        if (!status_.empty()) line(16, footer + 5, 336, 23, status_, colors.secondary);
    } else {
        const int footer = overviewFooter();
        if (expanded_) {
            const int top = overviewListTop + overviewDeviceHeight();
            rule(16, top, 352);
        }
        rule(0, footer, panelWidth);
        if (!status_.empty()) {
            line(16, footer - 34, available_.downloadPath.empty() ? 336 : 241, 24, status_, colors.secondary);
        }
    }
    EndPaint(panel_, &paint);
}

void App::drawSwitch(HDC dc, const RECT& box, const Palette& colors, bool on, bool disabled) const {
    const int width = px(36), height = px(18), inset = px(3);
    const int x = (box.left + box.right - width) / 2;
    const int y = (box.top + box.bottom - height) / 2;
    const auto off = dark() ? RGB(133, 133, 133) : colors.secondary;
    const auto track = CreateSolidBrush(disabled ? colors.border : on ? colors.accent : off);
    const auto oldBrush = SelectObject(dc, track);
    const auto oldPen = SelectObject(dc, GetStockObject(NULL_PEN));
    RoundRect(dc, x, y, x + width, y + height, height, height);
    const int dot = height - 2 * inset;
    const int dotX = on ? x + width - inset - dot : x + inset;
    const auto thumb = CreateSolidBrush(dark() && on && !disabled ? colors.ink : RGB(250, 251, 252));
    SelectObject(dc, thumb);
    Ellipse(dc, dotX, y + inset, dotX + dot, y + inset + dot);
    SelectObject(dc, oldBrush);
    SelectObject(dc, oldPen);
    DeleteObject(track);
    DeleteObject(thumb);
}

void App::drawItem(const DRAWITEMSTRUCT& item) {
    const HDC dc = item.hDC;
    const auto colors = palette(dark());
    const auto box = item.rcItem;
    SelectObject(dc, font_);
    if (item.CtlID == list) {
        const bool highlighted = (item.itemState & ODS_SELECTED) && (!settings_ || GetFocus() == item.hwndItem);
        fill(dc, box, highlighted ? colors.selected : colors.card);
        if (item.itemID == static_cast<UINT>(-1) || item.itemID >= devices_.size()) {
            if (devices_.empty()) {
                RECT label = box;
                label.left += px(12);
                label.bottom = label.top + px(settings_ ? 36 : 47);
                text(dc, label, settings_ ? L"未检测到设备" : L"未检测到受支持的设备", colors.secondary);
            }
            return;
        }
        const auto& device = devices_[item.itemID];
        const auto v = badge(device, dark());
        const auto accent = v.level == -1 || !device.fresh ? colors.secondary : batteryColor(v);
        if (settings_) {
            const int size = px(29);
            const int x = box.left + px(6), y = (box.top + box.bottom - size) / 2;
            if (const auto icon = batteryIcon(v, size)) {
                DrawIconEx(dc, x, y, icon, size, size, 0, nullptr, DI_NORMAL);
                DestroyIcon(icon);
            }
            const auto number = v.level >= 0 ? std::to_wstring(v.level) : L"--";
            const auto small = uiFont(px(10), FW_SEMIBOLD);
            const auto old = SelectObject(dc, small);
            text(dc, {x, y, x + size, y + size}, number, accent, DT_CENTER);
            SelectObject(dc, old);
            DeleteObject(small);
            const int menuLeft = box.right - px(deviceMenuWidth);
            const int switchLeft = menuLeft - px(deviceSwitchWidth);
            text(dc, {x + size + px(7), box.top, switchLeft - px(4), box.bottom},
                 name(device, true), colors.text);
            drawSwitch(dc, {switchLeft, box.top, menuLeft, box.bottom}, colors, ringVisible(device, true), false);
            text(dc, {menuLeft, box.top, box.right, box.bottom}, L"⋯", colors.secondary, DT_CENTER);
            fill(dc, {box.left, box.bottom - 1, box.right, box.bottom}, colors.border);
            if (item.itemState & ODS_FOCUS) DrawFocusRect(dc, &box);
            return;
        }
        const int left = box.left + px(12), right = box.right - px(deviceMenuWidth + 3);
        const int y = box.top;
        RECT menuBox{box.right - px(deviceMenuWidth), y, box.right, y + px(48)};
        text(dc, menuBox, L"⋯", colors.secondary, DT_CENTER);
        text(dc, {left, y + px(3), right - px(60), y + px(26)}, name(device), colors.text);
        text(dc, {right - px(62), y + px(3), right, y + px(26)}, displayLevel(device), accent, DT_RIGHT);
        auto state = description(device);
        if (!ringVisible(device)) state += L" · 圆环已隐藏";
        if (device.updated.time_since_epoch().count()) state += L" · " + clockText(device.updated);
        text(dc, {left, y + px(26), right, y + px(46)}, state, colors.secondary);
        fill(dc, {left, y + px(49), right, y + px(52)}, colors.border);
        if (v.level >= 0) fill(dc, {left, y + px(49), left + (right - left) * v.level / 100, y + px(52)}, accent);
        if (item.itemState & ODS_FOCUS) {
            RECT focus = box;
            InflateRect(&focus, -2, -2);
            DrawFocusRect(dc, &focus);
        }
        return;
    }
    const auto id = item.CtlID;
    const bool themeSelected = id == static_cast<UINT>(systemTheme + static_cast<int>(preview_.theme));
    const bool primary = id == save || id == refresh || themeSelected;
    const bool pressed = (item.itemState & ODS_SELECTED) != 0;
    const bool disabled = (item.itemState & ODS_DISABLED) != 0;
    if (id == quickRing || id == startup || id == globalRing) {
        const bool active = id == quickRing ? options_.rings : id == startup ? autostart_ : preview_.rings;
        fill(dc, box, pressed ? colors.selected : colors.surface);
        const int x = box.right - px(deviceSwitchWidth);
        drawSwitch(dc, {x, box.top, box.right, box.bottom}, colors, active, disabled);
        const auto* label = id == quickRing ? L"任务栏圆环" : id == startup ? L"开机自启" : L"全部圆环";
        text(dc, {box.left + px(9), box.top, x - px(4), box.bottom}, label, colors.text);
        if (item.itemState & ODS_FOCUS) DrawFocusRect(dc, &box);
        return;
    }
    const bool quiet = id == more || id == settingsTab || id == quit;
    fill(dc, box, primary ? colors.accent : pressed ? colors.selected : quiet ? colors.surface : colors.card);
    if (!primary && !quiet) {
        const auto border = CreateSolidBrush(colors.border);
        FrameRect(dc, &box, border);
        DeleteObject(border);
    }
    wchar_t caption[100]{};
    GetWindowTextW(item.hwndItem, caption, std::size(caption));
    text(dc, box, caption, disabled ? colors.secondary : primary ? colors.ink : colors.text, DT_CENTER);
    if (item.itemState & ODS_FOCUS) {
        RECT focus = box;
        InflateRect(&focus, -3, -3);
        DrawFocusRect(dc, &focus);
    }
}

void App::updateCheck() {
    if (checking_) return;
    if (updater_.joinable()) updater_.join();
    checking_ = true;
    status_ = L"正在检查更新…";
    available_ = {};
    placeControls();
    redraw();
    updater_ = std::thread([this] {
        auto result = checkForUpdate(&stop_);
        {
            std::lock_guard guard(lock_);
            release_ = std::move(result);
        }
        if (!stop_) PostMessageW(host_, updateMessage, 0, 0);
    });
}

void App::endUpdate() {
    if (updater_.joinable()) updater_.join();
    checking_ = false;
    status_ = release_.status;
    if (downloading_) {
        downloading_ = false;
        if (!staged_.empty()) {
            if (startUpdate(staged_, available_.digest)) {
                staged_.clear();
                PostMessageW(host_, WM_CLOSE, 0, 0);
                return;
            }
            status_ = L"无法启动更新程序";
        }
        staged_.clear();
    } else {
        available_ = release_;
    }
    if (!status_.empty()) diagnose(status_);
    if (settings_) positionPanel();
    placeControls();
    redraw();
}

void App::downloadUpdate() {
    if (checking_ || available_.downloadPath.empty()) return;
    keepOpen_ = true;
    const auto choice = MessageBoxW(panel_, L"下载并验证新版后，程序将退出并重新启动。现在更新吗？",
                                    L"外设电量", MB_YESNO | MB_DEFBUTTON2 | MB_ICONQUESTION);
    keepOpen_ = false;
    if (choice != IDYES) return;
    checking_ = downloading_ = true;
    status_ = L"正在下载并校验更新…";
    placeControls();
    redraw();
    updater_ = std::thread([this, selectedRelease = available_] {
        std::wstring error;
        const auto stage = stageUpdate(selectedRelease, error, &stop_);
        staged_ = stage;
        release_.status = std::move(error);
        if (!stop_) PostMessageW(host_, updateMessage, 0, 0);
    });
}

int writeReport(const std::filesystem::path& dest, const std::vector<Device>& devices) {
    std::ofstream out(dest, std::ios::binary);
    if (!out) return 3;
    for (const auto& device : devices) {
        out << toUtf8(device.name) << '\t' << toUtf8(displayLevel(device)) << '\t' << toUtf8(description(device)) << '\n';
    }
    return out ? 0 : 3;
}

} // namespace
} // namespace pbt

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int) {
    using namespace pbt;
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    int count = 0;
    auto args = CommandLineToArgvW(GetCommandLineW(), &count);
    if (args && count == 4 && wcscmp(args[1], L"--apply-update") == 0) {
        wchar_t* end = nullptr;
        const auto handle = wcstoull(args[2], &end, 10);
        const bool valid = handle != 0 && *end == L'\0';
        const auto digest = toUtf8(args[3]);
        LocalFree(args);
        return valid ? applyUpdate(reinterpret_cast<HANDLE>(static_cast<std::uintptr_t>(handle)), digest) : 5;
    }
    const bool openOnRelaunch = count == 1;
    const bool quietStart = args && count == 2
        && (wcscmp(args[1], L"--after-update") == 0 || wcscmp(args[1], L"--autostart") == 0);
    const bool probe = args && count == 3 && wcscmp(args[1], L"--probe") == 0;
    std::filesystem::path report = probe ? std::filesystem::path(args[2]) : std::filesystem::path{};
    if (args) LocalFree(args);

    const HANDLE mutex = CreateMutexW(nullptr, FALSE, mutexName);
    const DWORD mutexError = GetLastError();
    if (!mutex || mutexError == ERROR_ALREADY_EXISTS) {
        if (mutex) CloseHandle(mutex);
        if (mutexError == ERROR_ALREADY_EXISTS && openOnRelaunch) {
            if (const auto host = FindWindowW(hostClass, L"外设电量")) {
                DWORD processId{};
                if (GetWindowThreadProcessId(host, &processId)) {
                    AllowSetForegroundWindow(processId);
                    if (PostMessageW(host, reopenMessage, 0, 0)) return 0;
                }
            }
            if (const auto host = FindWindowW(L"PbtPythonHost", L"外设电量")) {
                DWORD processId{};
                if (GetWindowThreadProcessId(host, &processId)) {
                    AllowSetForegroundWindow(processId);
                    if (PostMessageW(host, WM_APP + 2, 0, 0)) return 0;
                }
            }
        }
        if (!probe && !quietStart) MessageBoxW(nullptr, L"已有外设电量实例正在运行。请先从托盘退出，再重新打开程序。",
                               L"外设电量", MB_OK | MB_ICONINFORMATION);
        return 2;
    }
    int result = 0;
    if (probe) {
        Scanner scanner;
        const std::atomic_bool stop(false);
        const Options defaults;
        result = writeReport(report, scanner.sample(stop, defaults.interval, defaults.retention, true));
    } else {
        Gdiplus::GdiplusStartupInput input;
        ULONG_PTR token{};
        if (Gdiplus::GdiplusStartup(&token, &input, nullptr) != Gdiplus::Ok) {
            CloseHandle(mutex);
            return 1;
        }
        {
            App app(instance, dataPath());
            if (app.start()) {
                MSG message{};
                while (GetMessageW(&message, nullptr, 0, 0) > 0) {
                    if (!app.handleDialogMessage(message)) {
                        TranslateMessage(&message);
                        DispatchMessageW(&message);
                    }
                }
            } else result = 1;
        }
        Gdiplus::GdiplusShutdown(token);
    }
    CloseHandle(mutex);
    return result;
}
