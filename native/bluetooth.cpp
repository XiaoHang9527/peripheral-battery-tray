#include "devices.hpp"

#include <windows.h>
#include <bluetoothapis.h>
#include <devpropdef.h>
#include <setupapi.h>

#include <array>
#include <bit>
#include <cstddef>

namespace pbt {
namespace {

constexpr std::wstring_view physicalPrefix = L"BTHENUM\\DEV_";
constexpr std::wstring_view handsFreePrefix = L"BTHENUM\\{0000111E-0000-1000-8000-00805F9B34FB}";
constexpr std::wstring_view headsetPrefix = L"BTHENUM\\{00001108-0000-1000-8000-00805F9B34FB}";
constexpr DEVPROPKEY containerIdKey{{0x8c7ed206, 0x3f8a, 0x4827, {0xb3, 0xab, 0xae, 0x9e, 0x1f, 0xae, 0xfc, 0x6c}}, 2};
// Windows publishes classic Bluetooth battery data on the Hands-Free devnode.
constexpr DEVPROPKEY batteryLevelKey{{0x104ea319, 0x6ee2, 0x4701, {0xbd, 0x47, 0x8d, 0xdb, 0xf4, 0x25, 0xbb, 0xe5}}, 2};
using ContainerId = std::array<std::byte, sizeof(GUID)>;

template<class T>
std::optional<T> property(HDEVINFO devices, SP_DEVINFO_DATA& node, const DEVPROPKEY& key, DEVPROPTYPE expected) {
    T value{};
    DEVPROPTYPE type{};
    DWORD written{};
    if (!SetupDiGetDevicePropertyW(devices, &node, &key, &type, reinterpret_cast<BYTE*>(&value), sizeof(value), &written, 0)
        || type != expected || written != sizeof(value)) return std::nullopt;
    return value;
}

std::optional<std::uint64_t> physicalAddress(std::wstring_view instance) {
    constexpr std::size_t digits = 12;
    if (!instance.starts_with(physicalPrefix) || instance.size() <= physicalPrefix.size() + digits
        || instance[physicalPrefix.size() + digits] != L'\\') return std::nullopt;
    std::uint64_t address = 0;
    for (const wchar_t digit : instance.substr(physicalPrefix.size(), digits)) {
        if (digit >= L'0' && digit <= L'9') address = (address << 4) | (digit - L'0');
        else if (digit >= L'A' && digit <= L'F') address = (address << 4) | (digit - L'A' + 10);
        else return std::nullopt;
    }
    return address ? std::optional{address} : std::nullopt;
}

struct HeadsetNodes {
    std::optional<std::uint64_t> address;
    std::optional<int> profileLevel, physicalLevel;
    bool hasAudioProfile = false;
};

} // namespace

std::vector<BluetoothHeadset> connectedHeadsets(const std::atomic_bool& stop) {
    if (stop) return {};
    BLUETOOTH_DEVICE_SEARCH_PARAMS search{};
    search.dwSize = sizeof(search);
    search.fReturnConnected = TRUE;
    BLUETOOTH_DEVICE_INFO info{};
    info.dwSize = sizeof(info);
    const auto found = BluetoothFindFirstDevice(&search, &info);
    if (!found) return {};
    struct Find {
        HBLUETOOTH_DEVICE_FIND handle;
        ~Find() { BluetoothFindDeviceClose(handle); }
    } radioSearch{found};
    std::map<std::uint64_t, std::wstring> connected;
    do {
        if (stop) return {};
        if (info.fConnected) connected.try_emplace(info.Address.ullLong, info.szName);
    } while (BluetoothFindNextDevice(found, &info));
    if (connected.empty()) return {};

    const auto devices = SetupDiGetClassDevsW(nullptr, L"BTHENUM", nullptr, DIGCF_PRESENT | DIGCF_ALLCLASSES);
    if (devices == INVALID_HANDLE_VALUE) return {};
    struct InfoSet {
        HDEVINFO handle;
        ~InfoSet() { SetupDiDestroyDeviceInfoList(handle); }
    } guard{devices};

    std::map<ContainerId, HeadsetNodes> groups;
    for (DWORD index = 0; !stop; ++index) {
        SP_DEVINFO_DATA node{};
        node.cbSize = sizeof(node);
        if (!SetupDiEnumDeviceInfo(devices, index, &node)) break;
        wchar_t id[512]{};
        if (!SetupDiGetDeviceInstanceIdW(devices, &node, id, std::size(id), nullptr)) continue;
        CharUpperBuffW(id, static_cast<DWORD>(wcslen(id)));
        const std::wstring_view instance(id);
        const auto address = physicalAddress(instance);
        const bool audioProfile = instance.starts_with(handsFreePrefix) || instance.starts_with(headsetPrefix);
        if (!audioProfile && (!address || !connected.contains(*address))) continue;
        const auto container = property<GUID>(devices, node, containerIdKey, DEVPROP_TYPE_GUID);
        if (!container) continue;
        const auto key = std::bit_cast<ContainerId>(*container);
        if (key == ContainerId{}) continue;
        auto& group = groups[key];
        const auto level = property<BYTE>(devices, node, batteryLevelKey, DEVPROP_TYPE_BYTE);
        if (address) {
            group.address = address;
            if (level && *level <= 100) group.physicalLevel = *level;
        } else {
            group.hasAudioProfile = true;
            if (level && *level <= 100) {
                // Prefer Hands-Free when a device exposes both Hands-Free and Headset profiles.
                if (!group.profileLevel || instance.starts_with(handsFreePrefix)) group.profileLevel = *level;
            }
        }
    }

    std::vector<BluetoothHeadset> result;
    for (const auto& [key, group] : groups) {
        if (!group.address || !group.hasAudioProfile) continue;
        const auto device = connected.find(*group.address);
        if (device == connected.end()) continue;
        result.push_back({*group.address, device->second.empty() ? L"蓝牙耳机" : device->second,
                          group.profileLevel ? group.profileLevel : group.physicalLevel});
    }
    return result;
}

} // namespace pbt
