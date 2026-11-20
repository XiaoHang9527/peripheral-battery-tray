#pragma once

#include "protocols.hpp"

#include <atomic>
#include <chrono>
#include <map>
#include <string>
#include <string_view>
#include <vector>

namespace pbt {

using Clock = std::chrono::steady_clock;

struct Device {
    std::wstring key;
    std::wstring name;
    Reading reading;
    bool present = false;
    bool fresh = false;
    Clock::time_point disconnected{};
    std::chrono::system_clock::time_point updated{};
    bool bluetooth = false;
    bool logitech = false;

    bool awaitingLogitechReply() const { return logitech && present && !fresh; }

    std::optional<int> displayLevel() const {
        return present && reading.wired ? std::nullopt : reading.level;
    }

    void acceptReading(Reading value, std::chrono::system_clock::time_point now) {
        // A wired Compx reply carries no percentage. Retain its last wireless reading.
        if (value.wired) value.level = reading.level;
        else updated = now;
        reading = value;
        fresh = true;
    }
};

struct HidInterface {
    std::wstring path, instance, product;
    unsigned vid, pid, page, usage, input, output, feature;
};

struct BluetoothHeadset {
    std::uint64_t address;
    std::wstring name;
    std::optional<int> level;
};

std::vector<HidInterface> enumerateHid();
std::vector<std::wstring> takeHidDiagnostics();
std::vector<BluetoothHeadset> connectedHeadsets(const std::atomic_bool& stop);
std::wstring fromUtf8(std::string_view input);
std::string toUtf8(std::wstring_view input);

class Scanner {
public:
    std::vector<Device> sample(const std::atomic_bool& stop, int interval, int retention, bool refresh,
                               bool retryLogitech = false);
    Clock::time_point nextWake(int retention) const;

private:
    struct Entry {
        Device device;
        Clock::time_point due{};
        unsigned failures = 0;
        std::map<unsigned, unsigned> features;
        std::optional<bool> percentage;
        bool named = false;
        bool kindKnown = false;
    };
    std::map<std::wstring, Entry> entries_;
    std::map<std::wstring, Clock::time_point> receiverScans_;
};

} // namespace pbt
