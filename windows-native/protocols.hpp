#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <numeric>
#include <optional>
#include <span>

namespace pbt {

using Report = std::span<const std::uint8_t>;
inline constexpr unsigned compxBatteryCommand = 0x4a;
inline constexpr unsigned compxModelCommand = 0x05;
inline constexpr std::size_t hidppLength = 20;
inline constexpr unsigned hidppNameFeature = 0x0005;

struct Reading {
    std::optional<int> level;
    bool charging = false;
    bool wired = false;
    bool fault = false;
    bool operator==(const Reading&) const = default;
};

inline std::uint8_t checksum(Report data) {
    return static_cast<std::uint8_t>(std::accumulate(data.begin(), data.end(), 0u));
}

inline bool compxReply(Report frame, unsigned command) {
    return frame.size() >= 20 && frame[0] == 0x13 && (frame[1] & 0x7f) == command
        && checksum(frame.first(19)) == frame[19];
}

inline std::optional<Reading> compxReading(Report frame) {
    if (!compxReply(frame, compxBatteryCommand)) return std::nullopt;
    if (frame[6] == 0x10) return Reading{std::nullopt, false, true};
    if (frame[6] == 1 && frame[5] >= 1 && frame[5] <= 100) return Reading{frame[5]};
    return std::nullopt;
}

inline const wchar_t* compxModelName(unsigned model) {
    switch (model) {
    case 0xcd: return L"AULA F75";
    case 0xa4: return L"AULA F99";
    default: return nullptr;
    }
}

inline const wchar_t* hidppDeviceKind(unsigned kind) {
    switch (kind) {
    case 0: return L"键盘";
    case 1: return L"遥控器";
    case 2: return L"数字键盘";
    case 3: return L"鼠标";
    case 4: return L"触控板";
    case 5: return L"轨迹球";
    case 6: return L"演示器";
    default: return L"外设";
    }
}

inline bool hidppReply(Report frame, unsigned slot, unsigned feature, unsigned command) {
    return frame.size() == hidppLength && frame[0] == 0x11 && frame[1] == slot
        && frame[2] == feature && frame[3] == command;
}

inline std::optional<Reading> hidppReading(Report frame, bool unified) {
    // HID++ 0x1000 and 0x1004 use different charge-state tables.
    // See Logitech's cpg-docs and hid-logitech-hidpp's unified battery mapping.
    if (frame.size() < (unified ? 8u : 7u) || frame[0] != 0x11 || frame[4] > 100
        || (!unified && frame[4] == 0)) return std::nullopt;
    const int status = frame[6];
    return Reading{frame[4], status >= 1 && status <= (unified ? 3 : 4), false,
                   unified ? status >= 4 : status >= 5};
}

inline bool needsHidppConfirmation(int level, std::optional<int> previous) {
    return !previous || std::abs(level - *previous) >= 25 || (level <= 5 && *previous > 10);
}

inline std::optional<Reading> confirmHidppReading(int first, std::optional<Reading> second,
                                                 std::optional<int> previous) {
    if (!second || !second->level) return std::nullopt;
    // Prefer a return to the previous range, or accept a change corroborated by the re-read.
    if (!needsHidppConfirmation(*second->level, previous) || std::abs(first - *second->level) <= 5) return second;
    return std::nullopt;
}

inline std::optional<Reading> rapooReading(Report frame, unsigned reportLength) {
    if (frame.size() != reportLength) return std::nullopt;
    unsigned state, level;
    if (frame.size() == 7 && frame[0] == 0xbb && frame[1] == 0xb0) {
        state = frame[5];
        level = frame[6];
        if (state == 2 && level == 0x7f) return Reading{std::nullopt, true};
    } else if ((frame.size() == 13 || frame.size() == 16 || frame.size() == 19)
               && frame[0] == 7 && (frame[1] & 0x0f) == 0) {
        state = frame[7];
        level = frame[8];
    } else {
        return std::nullopt;
    }
    if ((state != 1 && state != 2) || level > 100) return std::nullopt;
    return Reading{static_cast<int>(level), state == 2};
}

inline std::uint8_t razerChecksum(Report frame) {
    return std::accumulate(frame.begin() + 3, frame.begin() + 89, std::uint8_t{},
        [](std::uint8_t value, std::uint8_t byte) { return value ^ byte; });
}

inline std::optional<int> razerValue(Report reply, Report request) {
    if (reply.size() != 91 || request.size() != 91 || reply[0] != 0 || reply[1] != 2
        || reply[2] != request[2] || !std::equal(reply.begin() + 3, reply.begin() + 9, request.begin() + 3)
        || reply[89] != razerChecksum(reply)) return std::nullopt;
    return reply[10];
}

inline std::optional<Reading> sinowealthReading(Report reply) {
    if (reply.size() == 65 && reply[0] == 0 && reply[1] == 0xa1
        && reply[4] == 2 && reply[6] == 0x83 && reply[8] <= 100) {
        return Reading{reply[8], reply[7] == 1};
    }
    return std::nullopt;
}

} // namespace pbt
