#include "update.hpp"
#include "devices.hpp"
#include "preferences.hpp"

#include <winhttp.h>
#include <bcrypt.h>
#include <objbase.h>
#include <array>
#include <charconv>
#include <cmath>
#include <cstring>
#include <fstream>
#include <optional>

#include "third_party/picojson.h"

namespace pbt {
namespace fs = std::filesystem;
namespace {

constexpr std::size_t maxExecutable = 16 * 1024 * 1024;
constexpr auto releaseList = L"/repos/XiaoHang9527/peripheral-battery-tray/releases?per_page=30";
constexpr auto downloadPrefix = L"/XiaoHang9527/peripheral-battery-tray/releases/download/";
constexpr auto stagingPrefix = L".pbt-native-update-";
constexpr auto helperName = L"update-helper.exe";
constexpr auto completedName = L"completed";
constexpr auto backupName = L".pbt-native-previous.exe.bak";

struct Internet {
    HINTERNET handle;
    ~Internet() { if (handle) WinHttpCloseHandle(handle); }
};

struct Response {
    std::string bytes;
    DWORD code = 0;
};

Response get(const wchar_t* host, const std::wstring& path, std::size_t limit, bool json, const std::atomic_bool* stop) {
    if (stop && *stop) return {};
    const Internet session{WinHttpOpen(L"PeripheralBatteryTrayNative/0.1",
        WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0)};
    if (!session.handle) return {};
    WinHttpSetTimeouts(session.handle, 4000, 5000, 5000, 5000);
    const Internet connection{WinHttpConnect(session.handle, host, INTERNET_DEFAULT_HTTPS_PORT, 0)};
    if (!connection.handle) return {};
    const Internet request{WinHttpOpenRequest(connection.handle, L"GET", path.c_str(), nullptr, WINHTTP_NO_REFERER,
                              WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE)};
    if (!request.handle) return {};
    DWORD policy = WINHTTP_OPTION_REDIRECT_POLICY_DISALLOW_HTTPS_TO_HTTP;
    if (!WinHttpSetOption(request.handle, WINHTTP_OPTION_REDIRECT_POLICY, &policy, sizeof(policy))) return {};
    const auto headers = json ? L"Accept: application/vnd.github+json\r\n" : WINHTTP_NO_ADDITIONAL_HEADERS;
    if (!WinHttpSendRequest(request.handle, headers, json ? static_cast<DWORD>(-1) : 0,
                            WINHTTP_NO_REQUEST_DATA, 0, 0, 0)
        || !WinHttpReceiveResponse(request.handle, nullptr)) return {};
    Response result;
    DWORD size = sizeof(result.code);
    if (!WinHttpQueryHeaders(request.handle, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &result.code, &size, WINHTTP_NO_HEADER_INDEX)) return {};
    if (result.code != 200) return result;
    std::array<char, 32768> buffer{};
    const auto deadline = Clock::now() + std::chrono::seconds(json ? 30 : 180);
    for (;;) {
        DWORD count = 0;
        if ((stop && *stop) || Clock::now() >= deadline || !WinHttpReadData(request.handle, buffer.data(),
              static_cast<DWORD>(buffer.size()), &count) || result.bytes.size() + count > limit) return {};
        if (!count) return result;
        result.bytes.append(buffer.data(), count);
    }
}

std::optional<std::array<unsigned, 3>> semver(std::string_view input) {
    std::array<unsigned, 3> value{};
    for (std::size_t i = 0; i < value.size(); ++i) {
        const auto split = input.find('.');
        if ((i < 2) != (split != std::string_view::npos)) return std::nullopt;
        const auto number = input.substr(0, split);
        if (number.empty() || number.size() > 5 || (number.size() > 1 && number.front() == '0')) return std::nullopt;
        const auto [end, error] = std::from_chars(number.data(), number.data() + number.size(), value[i]);
        if (error != std::errc{} || end != number.data() + number.size()) return std::nullopt;
        if (i < 2) input.remove_prefix(split + 1);
    }
    return value;
}

std::string field(const picojson::value& value, const char* key) {
    if (!value.is<picojson::object>()) return {};
    const auto& object = value.get<picojson::object>();
    const auto it = object.find(key);
    return it != object.end() && it->second.is<std::string>() ? it->second.get<std::string>() : std::string{};
}

bool validDigest(std::string_view digest) {
    return digest.size() == 64 && std::all_of(digest.begin(), digest.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    });
}

std::string sha256(std::string_view data) {
    BCRYPT_ALG_HANDLE algorithm{};
    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0) return {};
    std::array<UCHAR, 32> digest{};
    const auto status = BCryptHash(algorithm, nullptr, 0,
        reinterpret_cast<PUCHAR>(const_cast<char*>(data.data())), static_cast<ULONG>(data.size()),
        digest.data(), static_cast<ULONG>(digest.size()));
    BCryptCloseAlgorithmProvider(algorithm, 0);
    if (status < 0) return {};
    std::string result;
    constexpr char hex[] = "0123456789abcdef";
    for (const auto octet : digest) {
        result.push_back(hex[octet >> 4]);
        result.push_back(hex[octet & 15]);
    }
    return result;
}

bool nativeExecutable(std::string_view data) {
    if (data.size() < sizeof(IMAGE_DOS_HEADER)) return false;
    IMAGE_DOS_HEADER dos{};
    memcpy(&dos, data.data(), sizeof(dos));
    if (dos.e_magic != IMAGE_DOS_SIGNATURE || dos.e_lfanew < static_cast<LONG>(sizeof(dos))
        || static_cast<std::size_t>(dos.e_lfanew) > data.size() - sizeof(dos)
        || data.size() - dos.e_lfanew < sizeof(IMAGE_NT_HEADERS64)) return false;
    IMAGE_NT_HEADERS64 image{};
    memcpy(&image, data.data() + dos.e_lfanew, sizeof(image));
    return image.Signature == IMAGE_NT_SIGNATURE && image.FileHeader.Machine == IMAGE_FILE_MACHINE_AMD64
        && image.OptionalHeader.Magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC && image.OptionalHeader.Subsystem == IMAGE_SUBSYSTEM_WINDOWS_GUI
        && (image.FileHeader.Characteristics & IMAGE_FILE_DLL) == 0;
}

void removeStage(const fs::path& stage) {
    std::error_code error;
    // The running helper cannot remove its own image until it exits.
    for (const auto* name : {L"download.exe", helperName}) fs::remove(stage / name, error);
    fs::remove(stage, error);
}

bool launch(const fs::path& path, std::wstring arguments, HANDLE parent, PROCESS_INFORMATION& info) {
    auto command = L"\"" + path.wstring() + L"\"" + arguments;
    if (!parent) {
        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        return CreateProcessW(path.c_str(), command.data(), nullptr, nullptr, FALSE, 0, nullptr,
                             path.parent_path().c_str(), &startup, &info);
    }
    SIZE_T size{};
    InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
    if (!size) return false;
    std::vector<std::byte> storage(size);
    const auto attributes = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());
    if (!InitializeProcThreadAttributeList(attributes, 1, 0, &size)) return false;
    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.lpAttributeList = attributes;
    const bool started = UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        &parent, sizeof(parent), nullptr, nullptr)
        && CreateProcessW(path.c_str(), command.data(), nullptr, nullptr, TRUE, EXTENDED_STARTUPINFO_PRESENT, nullptr,
                          path.parent_path().c_str(), &startup.StartupInfo, &info);
    DeleteProcThreadAttributeList(attributes);
    return started;
}

} // namespace

UpdateResult checkForUpdate(const std::atomic_bool* stop) {
    const auto response = get(L"api.github.com", releaseList, 2 * 1024 * 1024, true, stop);
    if (response.code != 200) return {L"无法连接 GitHub，请稍后重试", {}, {}, 0};
    picojson::value root;
    if (!picojson::parse(root, response.bytes).empty() || !root.is<picojson::array>()) {
        return {L"无法读取 GitHub 发布信息", {}, {}, 0};
    }
    constexpr std::string_view prefix = "native-v";
    const auto current = semver(toUtf8(version));
    if (!current) return {L"本机版本号格式无效", {}, {}, 0};
    auto newest = *current;
    UpdateResult result{L"当前没有可用的更新", {}, {}, 0};
    for (const auto& release : root.get<picojson::array>()) {
        const auto tag = field(release, "tag_name");
        if (!tag.starts_with(prefix)) continue;
        const auto number = semver(std::string_view(tag).substr(prefix.size()));
        if (!number || *number <= newest) continue;
        const auto& draft = release.get("draft");
        const auto& assets = release.get("assets");
        if (!draft.is<bool>() || draft.get<bool>() || !assets.is<picojson::array>()) continue;
        const auto path = std::wstring(downloadPrefix) + fromUtf8(tag) + L"/" + nativeExe;
        const auto url = toUtf8(L"https://github.com" + path);
        for (const auto& asset : assets.get<picojson::array>()) {
            if (field(asset, "name") != toUtf8(nativeExe) || field(asset, "browser_download_url") != url) continue;
            auto digest = field(asset, "digest");
            if (!digest.starts_with("sha256:")) continue;
            digest.erase(0, 7);
            const auto& bytes = asset.get("size");
            if (!validDigest(digest) || !bytes.is<double>()) continue;
            const double size = bytes.get<double>();
            if (size <= 0 || size > maxExecutable || std::floor(size) != size) continue;
            newest = *number;
            result = {L"发现新版本 " + fromUtf8(tag.substr(prefix.size())), path, digest, static_cast<std::size_t>(size)};
            break;
        }
    }
    return result;
}

void discardUpdate(const fs::path& stage) {
    if (!stage.empty()) removeStage(stage);
}

void cleanupUpdateStages() {
    std::error_code error;
    fs::directory_iterator it(executablePath().parent_path(), error), end;
    while (!error && it != end) {
        const auto stage = it->path();
        it.increment(error);
        if (!stage.filename().wstring().starts_with(stagingPrefix)) continue;
        const DWORD flags = GetFileAttributesW(stage.c_str());
        if (flags == INVALID_FILE_ATTRIBUTES || !(flags & FILE_ATTRIBUTE_DIRECTORY)
            || (flags & FILE_ATTRIBUTE_REPARSE_POINT)) continue;
        std::error_code cleanup;
        if (!fs::is_regular_file(stage / completedName, cleanup)) continue;
        fs::remove(stage / helperName, cleanup);
        if (cleanup) continue;
        fs::remove(stage / completedName, cleanup);
        fs::remove(stage, cleanup);
    }
}

fs::path stageUpdate(const UpdateResult& release, std::wstring& error, const std::atomic_bool* stop) {
    error.clear();
    const auto executable = executablePath();
    if (_wcsicmp(executable.filename().c_str(), nativeExe) != 0) {
        error = L"请保留原始程序文件名后再更新";
        return {};
    }
    if (!release.size || release.size > maxExecutable || !validDigest(release.digest)) {
        error = L"缺少可校验的更新文件";
        return {};
    }
    GUID id{};
    if (FAILED(CoCreateGuid(&id))) {
        error = L"无法准备更新目录";
        return {};
    }
    wchar_t suffix[40]{};
    StringFromGUID2(id, suffix, std::size(suffix));
    const auto stage = executable.parent_path() / (std::wstring(stagingPrefix) + suffix);
    std::error_code fileError;
    if (!fs::create_directory(stage, fileError)) {
        error = L"当前程序目录不可写，无法安装更新";
        return {};
    }
    const auto response = get(L"github.com", release.downloadPath, release.size, false, stop);
    if (response.code != 200) {
        error = response.code ? L"下载失败，GitHub 返回 HTTP " + std::to_wstring(response.code) : L"下载连接中断或超时";
    } else if (response.bytes.size() != release.size) error = L"下载文件大小与发布信息不符";
    else if (!nativeExecutable(response.bytes)) error = L"更新文件不是有效的 Windows x64 程序";
    else if (sha256(response.bytes) != release.digest) error = L"更新文件 SHA-256 校验未通过";
    if (!error.empty()) {
        removeStage(stage);
        return {};
    }
    std::ofstream output(stage / L"download.exe", std::ios::binary);
    output.write(response.bytes.data(), static_cast<std::streamsize>(response.bytes.size()));
    output.close();
    if (!output || !fs::copy_file(executable, stage / helperName, fileError)) {
        removeStage(stage);
        error = L"无法保存更新文件";
        return {};
    }
    return stage;
}

bool startUpdate(const fs::path& stage, const std::string& digest) {
    HANDLE parent{};
    const auto self = GetCurrentProcess();
    if (!DuplicateHandle(self, self, self, &parent, SYNCHRONIZE, TRUE, 0)) {
        removeStage(stage);
        return false;
    }
    const auto args = L" --apply-update " + std::to_wstring(reinterpret_cast<std::uintptr_t>(parent))
        + L" " + fromUtf8(digest);
    PROCESS_INFORMATION helper{};
    const bool started = launch(stage / helperName, args, parent, helper);
    CloseHandle(parent);
    if (started) {
        CloseHandle(helper.hThread);
        CloseHandle(helper.hProcess);
    } else {
        removeStage(stage);
    }
    return started;
}

int applyUpdate(HANDLE process, std::string_view digest) {
    const auto executable = executablePath();
    const auto stage = executable.parent_path();
    const auto destination = stage.parent_path() / nativeExe;
    if (executable.filename() != helperName || !stage.filename().wstring().starts_with(stagingPrefix) || !validDigest(digest)) {
        CloseHandle(process);
        return 5;
    }
    const auto wait = WaitForSingleObject(process, 30000);
    CloseHandle(process);
    if (wait != WAIT_OBJECT_0) return 5;

    const auto downloaded = stage / L"download.exe";
    const auto backup = stage / L"previous.exe";
    bool installed = false;
    std::error_code error;
    const auto size = fs::file_size(downloaded, error);
    if (!error && size > 0 && size <= maxExecutable) {
        std::string bytes(static_cast<std::size_t>(size), '\0');
        std::ifstream input(downloaded, std::ios::binary);
        if (input.read(bytes.data(), static_cast<std::streamsize>(bytes.size()))
            && input.peek() == std::char_traits<char>::eof() && nativeExecutable(bytes) && sha256(bytes) == digest) {
            input.close();
            installed = ReplaceFileW(destination.c_str(), downloaded.c_str(), backup.c_str(),
                                     REPLACEFILE_IGNORE_MERGE_ERRORS, nullptr, nullptr);
        }
    }

    const auto restart = [&](bool check) {
        PROCESS_INFORMATION next{};
        if (!launch(destination, L" --after-update", nullptr, next)) return false;
        CloseHandle(next.hThread);
        bool started = true;
        if (check) {
            const auto result = WaitForSingleObject(next.hProcess, 1500);
            DWORD code = 0;
            started = result == WAIT_TIMEOUT || (result == WAIT_OBJECT_0
                && GetExitCodeProcess(next.hProcess, &code) && code == 0);
        }
        CloseHandle(next.hProcess);
        return started;
    };
    if (installed && restart(true)) {
        fs::remove(downloaded, error);
        const auto previous = destination.parent_path() / backupName;
        if (MoveFileExW(backup.c_str(), previous.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            std::ofstream(stage / completedName).put('\n');
        }
        return 0;
    }
    if (fs::exists(backup, error)) {
        MoveFileExW(backup.c_str(), destination.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    }
    restart(false);
    MessageBoxW(nullptr, L"无法完成更新。可在更新目录中检查上一个版本的备份。",
                L"外设电量", MB_OK | MB_ICONWARNING);
    return 5;
}

} // namespace pbt
