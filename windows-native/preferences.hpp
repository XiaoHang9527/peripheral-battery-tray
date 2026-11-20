#pragma once

#include <windows.h>
#include <filesystem>
#include <map>
#include <string>

namespace pbt {

enum class Theme { system, light, dark };

struct Options {
    Theme theme = Theme::system;
    int interval = 60;
    int low = 20;
    int retention = 2;
    bool rings = true;
    int logSize = 512;
    int logCopies = 1;
    int logDays = 0;
};

std::filesystem::path executablePath();
std::filesystem::path dataPath();
std::wstring profileKey(std::wstring_view device);
bool setAutostart(bool enabled);
bool hasAutostart();
bool systemDark(bool taskbar);

class Preferences {
public:
    explicit Preferences(const std::filesystem::path& directory);
    Options load() const;
    bool save(const Options& options, const std::map<std::wstring, bool>& rings = {},
              const std::map<std::wstring, std::wstring>& aliases = {}) const;
    bool setRings(bool visible) const;
    std::wstring alias(const std::wstring& key) const;
    bool setAlias(const std::wstring& key, const std::wstring& name) const;
    bool ring(const std::wstring& key) const;
    bool setRing(const std::wstring& key, bool visible) const;

private:
    std::wstring read(const wchar_t* section, const wchar_t* key, const wchar_t* fallback) const;
    bool write(const wchar_t* section, const wchar_t* key, const wchar_t* value) const;
    int number(const wchar_t* key, int fallback, int low, int high) const;
    std::filesystem::path path_;
};

} // namespace pbt
