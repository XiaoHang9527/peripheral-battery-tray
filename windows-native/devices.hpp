#pragma once

#include "protocols.hpp"

#include <array>
#include <atomic>
#include <chrono>
#include <map>
#include <string>
#include <string_view>
#include <vector>

namespace pbt {

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

struct InputActivity {
    Clock::time_point last{}, resumed{};

    void observe(Clock::time_point now) {
        if (last == Clock::time_point{} || now - last >= 15s) resumed = now;
        last = now;
    }

    bool recent(Clock::time_point now) const {
        return last != Clock::time_point{} && now - last < 5s;
    }
};

using InputActivities = std::map<std::wstring, InputActivity>;

struct PollSchedule {
    Clock::time_point due{}, attempted{};

    bool ready(const InputActivity& activity, Clock::time_point now) const {
        return activity.recent(now) && activity.last > attempted && (now >= due || activity.resumed > attempted);
    }
};

struct ReceiverActivity {
    using Counters = std::array<std::uint8_t, 6>;
    std::optional<Counters> previous;
    std::array<InputActivity, 6> slots;
    std::array<PollSchedule, 6> discovery;
    std::array<Clock::time_point, 6> ignoreRepliesUntil{};
    Clock::time_point due{}, sampled{};

    void observe(const std::optional<Counters>& counters, Clock::time_point now) {
        if (counters && previous && now - sampled < 3s) {
            for (std::size_t index = 0; index < slots.size(); ++index) {
                if (sampled >= ignoreRepliesUntil[index] && (*counters)[index] != (*previous)[index]) {
                    slots[index].observe(now);
                }
            }
        }
        previous = counters;
        sampled = now;
        due = now + (counters ? 1s : 30s);
    }

    void queriedDevice(unsigned slot, Clock::time_point now) {
        // Ignore the receiver's count of any replies to our own battery and identity requests.
        ignoreRepliesUntil[slot - 1] = now + 500ms;
    }
};

struct Device {
    std::wstring key;
    std::wstring name;
    Reading reading;
    bool present = false;
    bool fresh = false;
    Clock::time_point disconnected{};
    std::chrono::system_clock::time_point updated{};
    bool bluetooth = false;
    bool readingUnconfirmed = false;
    bool operator==(const Device&) const = default;

    std::optional<int> displayLevel() const {
        return present && reading.wired ? std::nullopt : reading.level;
    }

    void acceptReading(Reading value, std::chrono::system_clock::time_point now) {
        // A wired Compx reply carries no percentage. Retain its last wireless reading.
        if (value.wired) value.level = reading.level;
        else updated = now;
        reading = value;
        fresh = true;
        readingUnconfirmed = false;
    }
};

struct HidInterface {
    std::wstring path, instance;
    std::array<std::uint8_t, 16> container;
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
                               bool rescan = false, const InputActivities& activity = {},
                               bool (*allowAutomatic)() = nullptr);
    Clock::time_point nextWake(int retention) const;

private:
    struct Entry {
        Device device;
        PollSchedule schedule;
        unsigned failures = 0;
        std::map<unsigned, unsigned> features;
        std::optional<bool> percentage;
        bool named = false;
        bool kindKnown = false;
    };
    std::map<std::wstring, Entry> entries_;
    std::map<std::wstring, ReceiverActivity> receiverActivity_;
    std::vector<HidInterface> interfaces_;
    std::vector<BluetoothHeadset> headsets_;
    Clock::time_point inventoryDue_{}, bluetoothDue_{};
};

} // namespace pbt
