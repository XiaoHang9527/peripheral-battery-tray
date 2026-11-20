#pragma once

#include <windows.h>
#include <atomic>
#include <cstddef>
#include <filesystem>
#include <string>

namespace pbt {

inline constexpr auto version = L"0.1.1";
inline constexpr auto nativeExe = L"peripheral-battery-tray-native.exe";

struct UpdateResult {
    std::wstring status;
    std::wstring downloadPath;
    std::string digest;
    std::size_t size = 0;
};

UpdateResult checkForUpdate(const std::atomic_bool* stop = nullptr);
std::filesystem::path stageUpdate(const UpdateResult& release, std::wstring& error, const std::atomic_bool* stop = nullptr);
bool startUpdate(const std::filesystem::path& stage, const std::string& digest);
void discardUpdate(const std::filesystem::path& stage);
void cleanupUpdateStages();
int applyUpdate(HANDLE process, std::string_view digest);

} // namespace pbt
