#include "preferences.hpp"

#include <shlobj.h>
#include <algorithm>
#include <fstream>
#include <iomanip>
#include <sstream>

namespace pbt {
namespace fs = std::filesystem;
namespace {

constexpr auto runKey = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
constexpr auto valueName = L"PeripheralBatteryTrayNative";
constexpr auto themes = L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize";

bool prepare(const fs::path& path) {
    std::error_code error;
    fs::create_directories(path, error);
    if (error) return false;
    const auto scratch = path / (L".write-" + std::to_wstring(GetCurrentProcessId()) + L".tmp");
    const auto probe = CreateFileW(scratch.c_str(), GENERIC_WRITE | DELETE, 0, nullptr, CREATE_NEW,
                                   FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, nullptr);
    if (probe == INVALID_HANDLE_VALUE) return false;
    CloseHandle(probe);
    return true;
}

std::wstring autostartValue() {
    return L"\"" + executablePath().wstring() + L"\" --autostart";
}

} // namespace

fs::path executablePath() {
    std::wstring path(32768, L'\0');
    const auto length = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
    path.resize(length);
    return fs::path(path);
}

fs::path dataPath() {
    const auto base = executablePath().parent_path();
    const auto portable = base / L".data" / L"native";
    std::error_code error;
    if (fs::exists(base / L"portable.mode", error)) return prepare(portable) ? portable : fs::path{};
    PWSTR local = nullptr;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr, &local))) {
        const auto path = fs::path(local) / valueName;
        CoTaskMemFree(local);
        if (prepare(path)) return path;
    }
    return prepare(portable) ? portable : fs::path{};
}

std::wstring profileKey(std::wstring_view device) {
    // A compact, stable INI section key independent of translated device names.
    std::uint64_t hash = 14695981039346656037ull;
    for (const wchar_t ch : device) {
        hash = (hash ^ static_cast<std::uint16_t>(ch)) * 1099511628211ull;
    }
    std::wostringstream out;
    out << L"device-" << std::hex << hash;
    return out.str();
}

bool hasAutostart() {
    wchar_t value[32768]{};
    DWORD size = sizeof(value);
    return RegGetValueW(HKEY_CURRENT_USER, runKey, valueName, RRF_RT_REG_SZ, nullptr, value, &size) == ERROR_SUCCESS
        && _wcsicmp(value, autostartValue().c_str()) == 0;
}

bool setAutostart(bool enabled) {
    if (!enabled && !hasAutostart()) return true;
    HKEY key{};
    if (RegOpenKeyExW(HKEY_CURRENT_USER, runKey, 0, KEY_SET_VALUE, &key) != ERROR_SUCCESS) return false;
    const auto value = autostartValue();
    const auto result = enabled ? RegSetValueExW(key, valueName, 0, REG_SZ,
        reinterpret_cast<const BYTE*>(value.c_str()), static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t)))
        : RegDeleteValueW(key, valueName);
    RegCloseKey(key);
    return result == ERROR_SUCCESS;
}

bool systemDark(bool taskbar) {
    DWORD value = 1;
    DWORD size = sizeof(value);
    RegGetValueW(HKEY_CURRENT_USER, themes, taskbar ? L"SystemUsesLightTheme" : L"AppsUseLightTheme",
                 RRF_RT_REG_DWORD, nullptr, &value, &size);
    return value == 0;
}

Preferences::Preferences(const fs::path& directory) {
    if (directory.empty()) return;
    path_ = directory / L"settings.ini";
    const auto file = CreateFileW(path_.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_NEW,
                                  FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file != INVALID_HANDLE_VALUE) {
        constexpr BYTE bom[] = {0xff, 0xfe};
        DWORD count = 0;
        WriteFile(file, bom, sizeof(bom), &count, nullptr);
        CloseHandle(file);
    }
}

std::wstring Preferences::read(const wchar_t* section, const wchar_t* key, const wchar_t* fallback) const {
    if (path_.empty()) return fallback;
    wchar_t text[256]{};
    GetPrivateProfileStringW(section, key, fallback, text, std::size(text), path_.c_str());
    return text;
}

bool Preferences::write(const wchar_t* section, const wchar_t* key, const wchar_t* value) const {
    return !path_.empty() && WritePrivateProfileStringW(section, key, value, path_.c_str());
}

int Preferences::number(const wchar_t* key, int fallback, int low, int high) const {
    const auto text = read(L"tray", key, L"");
    wchar_t* end = nullptr;
    const auto value = wcstol(text.c_str(), &end, 10);
    if (text.empty() || *end != L'\0' || value < low || value > high) return fallback;
    return static_cast<int>(value);
}

Options Preferences::load() const {
    return {static_cast<Theme>(number(L"theme", 0, 0, 2)), number(L"interval", Options{}.interval, 5, 300),
            number(L"low", 20, 5, 50), number(L"retention", 2, 0, 120), number(L"rings", 1, 0, 1) != 0,
            number(L"log_size_kb", 512, 64, 10240), number(L"log_copies", 1, 1, 9), number(L"log_days", 0, 0, 365)};
}

bool Preferences::save(const Options& options, const std::map<std::wstring, bool>& rings,
                       const std::map<std::wstring, std::wstring>& aliases) const {
    if (path_.empty()) return false;
    auto staged = *this;
    staged.path_ += L"." + std::to_wstring(GetCurrentProcessId()) + L".tmp";
    if (!CopyFileW(path_.c_str(), staged.path_.c_str(), FALSE)) return false;
    const auto number = [&](const wchar_t* key, int value) {
        return staged.write(L"tray", key, std::to_wstring(value).c_str());
    };
    const bool saved = number(L"theme", static_cast<int>(options.theme))
        && number(L"interval", options.interval) && number(L"low", options.low)
        && number(L"retention", options.retention) && staged.setRings(options.rings)
        && number(L"log_size_kb", options.logSize) && number(L"log_copies", options.logCopies)
        && number(L"log_days", options.logDays)
        && std::all_of(rings.begin(), rings.end(), [&](const auto& entry) { return staged.setRing(entry.first, entry.second); })
        && std::all_of(aliases.begin(), aliases.end(), [&](const auto& entry) { return staged.setAlias(entry.first, entry.second); });
    WritePrivateProfileStringW(nullptr, nullptr, nullptr, staged.path_.c_str());
    WritePrivateProfileStringW(nullptr, nullptr, nullptr, path_.c_str());
    if (saved && MoveFileExW(staged.path_.c_str(), path_.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) return true;
    DeleteFileW(staged.path_.c_str());
    return false;
}

bool Preferences::setRings(bool visible) const {
    return write(L"tray", L"rings", visible ? L"1" : L"0");
}

std::wstring Preferences::alias(const std::wstring& key) const {
    return read(profileKey(key).c_str(), L"name", L"");
}

bool Preferences::setAlias(const std::wstring& key, const std::wstring& name) const {
    return write(profileKey(key).c_str(), L"name", name.empty() ? nullptr : name.c_str());
}

bool Preferences::ring(const std::wstring& key) const {
    return read(profileKey(key).c_str(), L"ring", L"1") != L"0";
}

bool Preferences::setRing(const std::wstring& key, bool visible) const {
    return write(profileKey(key).c_str(), L"ring", visible ? L"1" : L"0");
}

} // namespace pbt
