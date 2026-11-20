#include "devices.hpp"

#include <windows.h>
#include <setupapi.h>
#include <hidsdi.h>

#include <array>
#include <future>
#include <memory>
#include <mutex>
#include <set>
#include <thread>
#include <utility>

namespace pbt {
namespace {

using namespace std::chrono_literals;
using Bytes = std::vector<std::uint8_t>;

struct HidLog {
    std::mutex lock;
    std::vector<std::wstring> messages;
    std::set<std::wstring> reported;
};

HidLog& hidLog() {
    // Vendor calls driven by a stalled driver can outlive the scanner thread.
    static auto* log = new HidLog;
    return *log;
}

void noteHid(std::wstring message, bool once = false) {
    auto& log = hidLog();
    std::lock_guard guard(log.lock);
    if (once && !log.reported.insert(message).second) return;
    log.messages.push_back(std::move(message));
}

class Handle {
public:
    explicit Handle(HANDLE handle) : value(handle) {}
    ~Handle() { if (*this) CloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    explicit operator bool() const { return value && value != INVALID_HANDLE_VALUE; }
    const HANDLE value;
};

HANDLE openHid(const std::wstring& path, DWORD access, bool overlapped = false) {
    return CreateFileW(path.c_str(), access, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                       overlapped ? FILE_FLAG_OVERLAPPED : 0, nullptr);
}

struct Transfer {
    explicit Transfer(const Bytes& packet) : buffer(packet) { state.hEvent = event.value; }
    Handle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
    Bytes buffer;
    OVERLAPPED state{};
};

DWORD transfer(const HidInterface& device, HANDLE handle, Bytes& packet, bool writing, Clock::time_point deadline,
               const std::atomic_bool& stop) {
    if (stop) return 0;
    // A cancelled request can outlive the scanner. Keep its storage until Windows signals it.
    using Pending = std::map<std::wstring, std::unique_ptr<Transfer>>;
    static auto& pending = *new Pending;
    std::erase_if(pending, [](const auto& item) { return WaitForSingleObject(item.second->event.value, 0) == WAIT_OBJECT_0; });
    if (pending.contains(device.instance)) return 0;
    const auto slot = pending.emplace(device.instance, std::make_unique<Transfer>(packet)).first;
    struct TransferSlot {
        Pending& map;
        Pending::iterator item;
        bool retained = false;
        ~TransferSlot() { if (!retained) map.erase(item); }
    } guard{pending, slot};
    auto& io = slot->second;
    if (!io->event) return 0;
    auto& op = io->state;
    const DWORD size = static_cast<DWORD>(packet.size());
    const BOOL started = writing ? WriteFile(handle, io->buffer.data(), size, nullptr, &op)
                                 : ReadFile(handle, io->buffer.data(), size, nullptr, &op);
    if (!started && GetLastError() != ERROR_IO_PENDING) return 0;
    while (!started && !stop && Clock::now() < deadline) {
        if (WaitForSingleObject(io->event.value, 30) == WAIT_OBJECT_0) break;
    }
    DWORD count = 0;
    if (!GetOverlappedResult(handle, &op, &count, FALSE)) {
        if (GetLastError() != ERROR_IO_INCOMPLETE) return 0;
        CancelIoEx(handle, &op);
        if (WaitForSingleObject(io->event.value, 250) != WAIT_OBJECT_0) {
            guard.retained = true;
            wchar_t label[16]{};
            swprintf(label, std::size(label), L"%04X:%04X", device.vid, device.pid);
            noteHid(std::wstring(label) + L" I/O 取消等待超时，暂停此接口读取");
            return 0;
        }
        if (!GetOverlappedResult(handle, &op, &count, FALSE)) return 0;
    }
    if (count > packet.size()) return 0;
    if (!writing) std::copy_n(io->buffer.begin(), count, packet.begin());
    return count;
}

template<typename Match>
Bytes receive(const HidInterface& device, HANDLE reader, Match match, Clock::time_point deadline,
              const std::atomic_bool& stop) {
    Bytes buffer(device.input);
    while (!stop && Clock::now() < deadline) {
        const auto count = transfer(device, reader, buffer, false, deadline, stop);
        if (!count) break;
        if (match(Report(buffer).first(count))) {
            buffer.resize(count);
            return buffer;
        }
    }
    return {};
}

class Link {
public:
    Link(const HidInterface& device, const std::atomic_bool& stop)
        : reader_(openHid(device.path, GENERIC_READ, true)),
          writer_(openHid(device.path, GENERIC_WRITE, true)),
          device_(device), stop_(stop) {}

    template<typename Match>
    Bytes exchange(Bytes request, Match match, std::chrono::milliseconds timeout) const {
        if (!reader_ || !writer_ || stop_) return {};
        HidD_FlushQueue(reader_.value);
        const auto deadline = Clock::now() + timeout;
        if (transfer(device_, writer_.value, request, true, deadline, stop_) != request.size()) return {};
        return receive(device_, reader_.value, match, deadline, stop_);
    }
private:
    Handle reader_, writer_;
    const HidInterface& device_;
    const std::atomic_bool& stop_;
};

bool waitForDevice(std::chrono::milliseconds delay, const std::atomic_bool& stop) {
    const auto deadline = Clock::now() + delay;
    while (!stop) {
        const auto remaining = deadline - Clock::now();
        if (remaining <= Clock::duration::zero()) return true;
        std::this_thread::sleep_for(std::min(remaining, std::chrono::duration_cast<Clock::duration>(25ms)));
    }
    return false;
}

Bytes askCompx(const Link& link, unsigned command, const std::atomic_bool& stop) {
    Bytes packet(20);
    packet[0] = 0x13;
    packet[1] = static_cast<std::uint8_t>(command);
    packet[2] = command == compxModelCommand ? 1 : 0;
    packet[19] = checksum(Report(packet).first(19));
    const bool model = command == compxModelCommand;
    const int attempts = model ? 2 : 3;
    for (int attempt = 0; attempt < attempts && !stop; ++attempt) {
        auto reply = link.exchange(packet, [command](Report r) { return compxReply(r, command); }, model ? 600ms : 700ms);
        if (!reply.empty()) return reply;
        if (attempt + 1 < attempts && !waitForDevice(200ms, stop)) break;
    }
    return {};
}

Bytes askHidpp(const Link& link, unsigned slot, unsigned feature, unsigned function,
               std::initializer_list<unsigned> parameters = {}, std::chrono::milliseconds timeout = 320ms) {
    Bytes packet(hidppLength);
    packet[0] = 0x11;
    packet[1] = static_cast<std::uint8_t>(slot);
    packet[2] = static_cast<std::uint8_t>(feature);
    const auto command = static_cast<std::uint8_t>((function << 4) | 1);
    packet[3] = command;
    std::copy(parameters.begin(), parameters.end(), packet.begin() + 4);
    return link.exchange(std::move(packet), [=](Report r) {
        return r.size() >= 7 && r[0] == 0x11 && r[1] == slot && r[2] == feature && r[3] == command;
    }, timeout);
}

std::optional<unsigned> hidppFeature(const Link& link, unsigned slot, unsigned feature,
                                    std::map<unsigned, unsigned>& cache) {
    if (const auto it = cache.find(feature); it != cache.end()) return it->second;
    const auto report = askHidpp(link, slot, 0, 0, {feature >> 8, feature & 255});
    if (report.empty()) return std::nullopt;
    return cache[feature] = report[4];
}

std::optional<Reading> readHidpp(const Link& link, unsigned slot, std::map<unsigned, unsigned>& cache,
                                std::optional<bool>& percentage) {
    const auto legacy = hidppFeature(link, slot, 0x1000, cache);
    if (legacy && *legacy != 0) {
        if (auto value = hidppReading(askHidpp(link, slot, *legacy, 0), false)) return value;
    }
    const auto unified = hidppFeature(link, slot, 0x1004, cache);
    if (!unified || *unified == 0) return std::nullopt;
    if (!percentage) {
        const auto caps = askHidpp(link, slot, *unified, 0);
        if (caps.empty()) return std::nullopt;
        percentage = (caps[5] & 2) != 0;
    }
    if (!*percentage) return std::nullopt;
    return hidppReading(askHidpp(link, slot, *unified, 1), true);
}

std::wstring readHidppName(const Link& link, unsigned slot, unsigned feature) {
    const auto info = askHidpp(link, slot, feature, 0);
    if (info.empty() || info[4] == 0) return {};
    std::string name;
    while (name.size() < info[4]) {
        const auto part = askHidpp(link, slot, feature, 1, {static_cast<unsigned>(name.size())});
        if (part.empty()) return {};
        name.append(reinterpret_cast<const char*>(part.data() + 4),
                    std::min<std::size_t>(part.size() - 4, info[4] - name.size()));
    }
    if (const auto end = name.find('\0'); end != std::string::npos) name.resize(end);
    const auto first = name.find_first_not_of(' ');
    if (first == std::string::npos) return {};
    return fromUtf8(std::string_view(name).substr(first, name.find_last_not_of(' ') - first + 1));
}

struct Vendor {
    unsigned vid, pid;
    const wchar_t* name;
    unsigned feature, transaction, interface, page;
};

constexpr Vendor vendors[] = {
    {0x1532, 0x007b, L"Razer Viper Ultimate", 91, 0x3f, 2, 0},
    {0x1532, 0x0072, L"Razer Mamba Wireless", 91, 0x3f, 2, 0},
    {0x1532, 0x0088, L"Razer Basilisk Ultimate", 91, 0x3f, 2, 0},
    {0x1532, 0x00a6, L"Razer Viper V2 Pro", 91, 0x1f, 0, 0},
    {0x1532, 0x00b6, L"Razer DeathAdder V3 Pro", 91, 0x1f, 0, 0},
    {0x258a, 0x2022, L"Glorious Model O Wireless", 65, 0, 0, 0},
    {0x258a, 0x2023, L"Glorious Model D Wireless", 65, 0, 0, 0},
    {0x258a, 0x2024, L"Glorious Model O- Wireless", 65, 0, 0, 0},
    {0x258a, 0x2025, L"Glorious Model D- Wireless", 65, 0, 0, 0},
    {0x36a7, 0xa874, L"WLMouse YING", 65, 0, 0, 0xffff},
};

const Vendor* matchVendor(const HidInterface& device) {
    for (const auto& model : vendors) {
        if (device.vid != model.vid || device.pid != model.pid || device.feature != model.feature) continue;
        if (model.page && (device.page != model.page || device.usage != 0)) continue;
        if (model.transaction) {
            const auto first = device.instance.find(L'\\');
            const auto last = device.instance.rfind(L'\\');
            if (first == std::wstring::npos || last == first) continue;
            const auto id = device.instance.substr(first + 1, last - first - 1);
            if (id.find(model.interface == 2 ? L"&MI_02" : L"&MI_00") == std::wstring::npos) continue;
        }
        return &model;
    }
    return nullptr;
}

std::wstring physicalCollection(const std::wstring& instance) {
    const auto last = instance.rfind(L'\\');
    const auto index = instance.find(L"&COL");
    if (last == std::wstring::npos || index == std::wstring::npos || index > last) return instance;
    const auto suffix = instance.rfind(L'&');
    return instance.substr(0, index) + instance.substr(last, suffix > last ? suffix - last : std::wstring::npos);
}

const wchar_t* matchRapoo(const HidInterface& d) {
    if (d.vid != 0x24ae || d.page != 0xff00 || d.usage != 2) return nullptr;
    switch (d.pid) {
    case 0x1215: return d.input == 7 ? L"Rapoo VT3 Pro" : nullptr;
    case 0x1233: return d.input == 7 ? L"Rapoo VT1 Pro Max" : nullptr;
    case 0x1218: return d.input == 13 || d.input == 16 || d.input == 19 ? L"Rapoo VT0" : nullptr;
    case 0x1243: return d.input == 16 ? L"Rapoo VT9 第二代" : nullptr;
    case 0x124d: return d.input == 16 ? L"Rapoo VT3 Max 第二代" : nullptr;
    default: return nullptr;
    }
}

Bytes featureExchange(HANDLE handle, Bytes packet, std::chrono::milliseconds delay) {
    if (!HidD_SetFeature(handle, packet.data(), static_cast<ULONG>(packet.size()))) return {};
    std::this_thread::sleep_for(delay);
    std::fill(packet.begin() + 1, packet.end(), 0);
    if (!HidD_GetFeature(handle, packet.data(), static_cast<ULONG>(packet.size()))) return {};
    return packet;
}

std::optional<Reading> queryVendor(const std::wstring& path, const Vendor& model) {
    auto handle = openHid(path, GENERIC_READ | GENERIC_WRITE);
    if (handle == INVALID_HANDLE_VALUE) {
        const auto fullAccessError = GetLastError();
        handle = openHid(path, 0);
        const auto fallbackError = handle == INVALID_HANDLE_VALUE ? GetLastError() : 0;
        auto message = std::wstring(model.name) + L" Feature 读写打开失败 WinError " + std::to_wstring(fullAccessError);
        message += fallbackError ? L"，零访问权限也失败 WinError " + std::to_wstring(fallbackError) : L"，已回退到零访问权限";
        noteHid(std::move(message), true);
    }
    const Handle featureDevice(handle);
    if (!featureDevice) return std::nullopt;
    if (model.transaction) {
        const auto read = [&](unsigned command) {
            Bytes request(91);
            request[2] = static_cast<std::uint8_t>(model.transaction);
            request[6] = 2;
            request[7] = 7;
            request[8] = static_cast<std::uint8_t>(command);
            request[89] = razerChecksum(request);
            return razerValue(featureExchange(featureDevice.value, request, 20ms), request);
        };
        const auto level = read(0x80);
        if (!level) return std::nullopt;
        return Reading{(*level * 100 + 127) / 255, read(0x84) == 1};
    }
    Bytes request(65);
    request[3] = request[4] = 2;
    request[6] = 0x83;
    return sinowealthReading(featureExchange(featureDevice.value, std::move(request), model.page ? 100ms : 50ms));
}

std::optional<Reading> readVendor(const HidInterface& device, const Vendor& model) {
    // Each interface can retain one outstanding synchronous call without blocking other devices.
    static std::map<std::wstring, std::future<std::optional<Reading>>> pending;
    std::erase_if(pending, [](const auto& item) { return item.second.wait_for(0ms) == std::future_status::ready; });
    if (pending.contains(device.instance)) return std::nullopt;
    try {
        std::packaged_task query([path = device.path, model] {
            return queryVendor(path, model);
        });
        auto reply = query.get_future();
        std::thread(std::move(query)).detach();
        if (reply.wait_for(1500ms) == std::future_status::ready) return reply.get();
        pending.emplace(device.instance, std::move(reply));
        noteHid(std::wstring(model.name) + L" Feature 读取超时，等待此接口的请求结束");
    } catch (const std::system_error&) {
        noteHid(std::wstring(model.name) + L" Feature 读取线程启动失败", true);
    }
    return std::nullopt;
}

struct Candidate {
    enum class Protocol { compx, logitech, rapoo, vendor };
    const HidInterface& hid;
    Protocol protocol;
    std::wstring name;
    unsigned slot = 0;
    const Vendor* vendor = nullptr;
};

} // namespace

std::wstring fromUtf8(std::string_view input) {
    if (input.empty()) return {};
    const auto size = static_cast<int>(input.size());
    const auto count = MultiByteToWideChar(CP_UTF8, 0, input.data(), size, nullptr, 0);
    std::wstring text(count, L'\0');
    if (count) MultiByteToWideChar(CP_UTF8, 0, input.data(), size, text.data(), count);
    return text;
}

std::string toUtf8(std::wstring_view input) {
    if (input.empty()) return {};
    const auto size = static_cast<int>(input.size());
    const auto count = WideCharToMultiByte(CP_UTF8, 0, input.data(), size, nullptr, 0, nullptr, nullptr);
    std::string text(count, '\0');
    if (count) WideCharToMultiByte(CP_UTF8, 0, input.data(), size, text.data(), count, nullptr, nullptr);
    return text;
}

std::vector<HidInterface> enumerateHid() {
    GUID guid{};
    HidD_GetHidGuid(&guid);
    const auto info = SetupDiGetClassDevsW(&guid, nullptr, nullptr, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (info == INVALID_HANDLE_VALUE) {
        noteHid(L"HID 枚举初始化失败 WinError " + std::to_wstring(GetLastError()), true);
        return {};
    }
    struct InfoSet {
        HDEVINFO handle;
        ~InfoSet() { SetupDiDestroyDeviceInfoList(handle); }
    } guard{info};
    std::vector<HidInterface> devices;
    for (DWORD index = 0;; ++index) {
        SP_DEVICE_INTERFACE_DATA interfaceData{};
        interfaceData.cbSize = sizeof(interfaceData);
        if (!SetupDiEnumDeviceInterfaces(info, nullptr, &guid, index, &interfaceData)) {
            const auto error = GetLastError();
            if (error != ERROR_NO_MORE_ITEMS) noteHid(L"HID 枚举中断 WinError " + std::to_wstring(error), true);
            break;
        }
        DWORD size = 0;
        SetupDiGetDeviceInterfaceDetailW(info, &interfaceData, nullptr, 0, &size, nullptr);
        if (size < sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)) continue;
        std::vector<std::byte> bytes(size);
        auto* detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W*>(bytes.data());
        detail->cbSize = sizeof(*detail);
        SP_DEVINFO_DATA data{};
        data.cbSize = sizeof(data);
        if (!SetupDiGetDeviceInterfaceDetailW(info, &interfaceData, detail, size, nullptr, &data)) continue;
        wchar_t instance[512]{};
        if (!SetupDiGetDeviceInstanceIdW(info, &data, instance, std::size(instance), nullptr)) continue;
        CharUpperBuffW(instance, static_cast<DWORD>(wcslen(instance)));
        const Handle hid(openHid(detail->DevicePath, 0));
        if (!hid) continue;
        HIDD_ATTRIBUTES attributes{};
        attributes.Size = sizeof(attributes);
        if (!HidD_GetAttributes(hid.value, &attributes)) continue;
        PHIDP_PREPARSED_DATA preparsed{};
        if (!HidD_GetPreparsedData(hid.value, &preparsed)) continue;
        HIDP_CAPS caps{};
        const auto status = HidP_GetCaps(preparsed, &caps);
        HidD_FreePreparsedData(preparsed);
        if (status != HIDP_STATUS_SUCCESS) continue;
        wchar_t product[128]{};
        HidD_GetProductString(hid.value, product, sizeof(product));
        devices.push_back({detail->DevicePath, instance, product, attributes.VendorID, attributes.ProductID,
                           caps.UsagePage, caps.Usage, caps.InputReportByteLength,
                           caps.OutputReportByteLength, caps.FeatureReportByteLength});
    }
    return devices;
}

std::vector<std::wstring> takeHidDiagnostics() {
    auto& log = hidLog();
    std::lock_guard guard(log.lock);
    return std::exchange(log.messages, {});
}

std::vector<Device> Scanner::sample(const std::atomic_bool& stop, int interval, int retention, bool refresh,
                                    bool retryLogitech) {
    using Protocol = Candidate::Protocol;
    const auto interfaces = enumerateHid();
    const HidInterface* compx = nullptr;
    std::map<std::wstring, std::vector<Candidate>> vendorCollections;
    std::vector<Candidate> candidates;
    std::set<std::wstring> receivers;
    for (const auto& hid : interfaces) {
        if (stop) return {};
        if (hid.vid == 0x3554 && (hid.pid == 0xfa09 || hid.pid == 0xfb40) && hid.usage == 2
            && hid.input == 20 && hid.output == 20 && (hid.page == 0xff02 || hid.page == 0xff04)) {
            if (!compx || (hid.pid == 0xfa09 && hid.page == 0xff02)) compx = &hid;
        } else if (hid.vid == 0x046d && hid.page == 0xff00 && hid.input == 20 && hid.output == 20) {
            if (!receivers.insert(hid.instance).second) continue;
            const bool probe = refresh || Clock::now() >= receiverScans_[hid.instance];
            const Link discovery(hid, stop);
            const auto scanSlots = [&](std::chrono::milliseconds timeout) {
                for (unsigned slot = 1; slot <= 6 && !stop; ++slot) {
                    const auto key = hid.instance + L":" + std::to_wstring(slot);
                    if (!entries_.contains(key)) {
                        if (!probe) continue;
                        const auto response = askHidpp(discovery, slot, 0, 1, {}, timeout);
                        if (response.empty() || (response[4] != 2 && response[4] != 4)) continue;
                    }
                    candidates.push_back({hid, Protocol::logitech, L"Logitech 设备 " + std::to_wstring(slot), slot});
                }
            };
            const auto begin = candidates.size();
            scanSlots(200ms);
            // A receiver may need one extra pass before a paired device answers.
            if (probe && candidates.size() == begin && !stop) scanSlots(350ms);
            if (probe) receiverScans_[hid.instance] = Clock::now() + 60s;
        } else if (const auto* name = matchRapoo(hid)) {
            candidates.push_back({hid, Protocol::rapoo, name});
        } else if (const auto* model = matchVendor(hid)) {
            vendorCollections[physicalCollection(hid.instance)].push_back({hid, Protocol::vendor, model->name, 0, model});
        }
    }
    std::erase_if(receiverScans_, [&](const auto& item) { return !receivers.contains(item.first); });
    if (compx) candidates.push_back({*compx, Protocol::compx, L"2.4G 键盘"});
    for (const auto& [key, matches] : vendorCollections) {
        if (matches.size() == 1) candidates.push_back(matches.front());
        else noteHid(matches.front().name + L" 命中多个候选 HID 接口，已跳过歧义查询", true);
    }

    std::set<std::wstring> found;
    for (const auto& candidate : candidates) {
        if (stop) return {};
        const auto& hid = candidate.hid;
        const auto key = candidate.slot ? hid.instance + L":" + std::to_wstring(candidate.slot) : hid.instance;
        if (!found.insert(key).second) continue;
        auto& state = entries_[key];
        auto& device = state.device;
        const bool logitech = candidate.protocol == Protocol::logitech;
        if (device.key.empty()) {
            device.key = key;
            device.name = candidate.name;
            device.logitech = logitech;
        }
        if (!device.present) {
            device.disconnected = {};
            state.due = {};
            state.named = state.kindKnown = false;
        }
        if (!device.present || refresh) {
            state.features.clear();
            state.percentage.reset();
            state.failures = 0;
            if (logitech) state.named = state.kindKnown = false;
        }
        device.present = true;
        if (!refresh && Clock::now() < state.due && !(retryLogitech && device.awaitingLogitechReply())) continue;

        std::optional<Reading> reading;
        switch (candidate.protocol) {
        case Protocol::compx: {
            const Link link(hid, stop);
            if (!state.named) {
                const auto reply = askCompx(link, compxModelCommand, stop);
                if (!reply.empty()) {
                    if (const auto* model = compxModelName(reply[10])) device.name = model;
                    else if (!hid.product.empty()) device.name = hid.product;
                    else {
                        wchar_t model[16]{};
                        swprintf(model, std::size(model), L"型号 0x%02X", static_cast<unsigned>(reply[10]));
                        device.name = model;
                    }
                    state.named = true;
                }
            }
            reading = compxReading(askCompx(link, compxBatteryCommand, stop));
            break;
        }
        case Protocol::logitech: {
            const Link link(hid, stop);
            if (!state.named) {
                const auto identity = hidppFeature(link, candidate.slot, hidppNameFeature, state.features);
                if (identity && *identity != 0) {
                    if (!state.kindKnown) {
                        const auto kind = askHidpp(link, candidate.slot, *identity, 2);
                        if (!kind.empty()) {
                            if (device.name == candidate.name) {
                                device.name = L"Logitech " + std::wstring(hidppDeviceKind(kind[4]))
                                    + L" " + std::to_wstring(candidate.slot);
                            }
                            state.kindKnown = true;
                        }
                    }
                    if (auto name = readHidppName(link, candidate.slot, *identity); !name.empty()) {
                        device.name = std::move(name);
                        state.named = true;
                    }
                }
            }
            reading = readHidpp(link, candidate.slot, state.features, state.percentage);
            if (reading && device.updated.time_since_epoch().count() == 0 && waitForDevice(150ms, stop)) {
                if (const auto confirmed = readHidpp(link, candidate.slot, state.features, state.percentage)) reading = confirmed;
            }
            break;
        }
        case Protocol::rapoo: {
            const Handle reader(openHid(hid.path, GENERIC_READ, true));
            if (reader) {
                const auto data = receive(hid, reader.value, [&](Report r) {
                    return rapooReading(r, hid.input).has_value();
                }, Clock::now() + 5s, stop);
                reading = rapooReading(data, hid.input);
            }
            break;
        }
        case Protocol::vendor:
            reading = readVendor(hid, *candidate.vendor);
            break;
        }

        if (reading) {
            device.acceptReading(*reading, std::chrono::system_clock::now());
            state.failures = 0;
        } else {
            device.fresh = false;
            state.failures = std::min(state.failures + 1, 4u);
            if (logitech) {
                // Re-resolve feature indexes after the slot stops answering.
                state.features.clear();
                state.percentage.reset();
            }
        }
        constexpr int backoff[] = {0, 60, 120, 180, 300};
        constexpr int logitechBackoff[] = {0, 15, 30, 60, 60};
        const int delay = state.failures ? (logitech ? logitechBackoff : backoff)[state.failures] : interval;
        state.due = Clock::now() + std::chrono::seconds(delay);
    }

    const auto headsets = connectedHeadsets(stop);
    if (stop) return {};
    const auto now = Clock::now();
    for (const auto& headset : headsets) {
        const auto key = L"bluetooth:" + std::to_wstring(headset.address);
        if (!found.insert(key).second) continue;
        auto& state = entries_[key];
        auto& device = state.device;
        const bool reconnected = !device.present;
        device.key = key;
        device.name = headset.name;
        device.bluetooth = device.present = true;
        device.disconnected = {};
        if (!refresh && !reconnected && now < state.due && device.fresh == headset.level.has_value()
            && (!headset.level || headset.level == device.reading.level)) continue;
        if (headset.level) device.acceptReading(Reading{headset.level}, std::chrono::system_clock::now());
        else device.fresh = false;
        state.due = now + std::chrono::seconds(interval);
    }

    std::vector<Device> result;
    for (auto it = entries_.begin(); it != entries_.end();) {
        auto& device = it->second.device;
        if (!found.contains(it->first) && device.present) {
            device.present = device.fresh = false;
            device.disconnected = now;
        }
        if (!device.present && now >= device.disconnected + std::chrono::minutes(retention)) {
            it = entries_.erase(it);
        } else {
            result.push_back(device);
            ++it;
        }
    }
    return result;
}

Clock::time_point Scanner::nextWake(int retention) const {
    auto wake = Clock::now() + std::chrono::seconds(30);
    for (const auto& [key, entry] : entries_) {
        wake = std::min(wake, entry.device.present ? entry.due
            : entry.device.disconnected + std::chrono::minutes(retention));
    }
    for (const auto& [key, due] : receiverScans_) wake = std::min(wake, due);
    return wake;
}

} // namespace pbt
