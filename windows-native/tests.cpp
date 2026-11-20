#include "devices.hpp"

#include <array>
#include <cassert>
#include <iostream>

using namespace pbt;

int main() {
    const auto start = Clock::time_point{20s};
    ReceiverActivity receiver;
    ReceiverActivity::Counters counts{};
    counts[0] = 255;
    receiver.observe(counts, start);
    assert(!receiver.slots[0].recent(start));
    counts[0] = 0; // The receiver's byte counter can wrap.
    receiver.observe(counts, start + 1s);
    assert(receiver.slots[0].recent(start + 1s));
    assert(!receiver.slots[1].recent(start + 1s));
    PollSchedule schedule{start + 60s, start + 1s};
    assert(!schedule.ready(receiver.slots[0], start + 1s));
    receiver.queriedDevice(1, start + 1s);
    ++counts[0]; // A reply to our own request must not restart the schedule.
    ++counts[1]; // Real activity on a different slot must still be seen.
    receiver.observe(counts, start + 2s);
    assert(receiver.slots[0].last == start + 1s);
    assert(receiver.slots[1].last == start + 2s);
    receiver.observe(counts, start + 20s);
    ++counts[0];
    receiver.observe(counts, start + 21s);
    assert(schedule.ready(receiver.slots[0], start + 21s));
    assert(!schedule.ready(receiver.slots[0], start + 26s));
    assert(!schedule.ready(receiver.slots[1], start + 60s));

    std::array<std::uint8_t, 20> compx{0x13, 0xca, 0, 0, 0, 68, 1};
    compx[19] = checksum(Report(compx).first(19));
    assert(compxReading(compx)->level == 68);
    ++compx[19];
    assert(!compxReading(compx));
    compx[6] = 0x10;
    compx[19] = checksum(Report(compx).first(19));
    assert(compxReading(compx)->wired && !compxReading(compx)->level);
    assert(std::wstring_view(compxModelName(0xcd)) == L"AULA F75");
    assert(std::wstring_view(compxModelName(0xa4)) == L"AULA F99");
    assert(!compxModelName(0xff));

    std::array<std::uint8_t, 20> hidpp{0x11, 2, 5, 1, 42, 0, 5};
    assert(hidppReply(hidpp, 2, 5, 1));
    assert(!hidppReply(hidpp, 2, 5, 2)); // Late answer from a previous query.
    assert(!hidppReply(hidpp, 2, 5, 0x11)); // Reply to a different function.
    assert(!hidppReply(Report(hidpp).first(7), 2, 5, 1));
    assert(hidppReading(hidpp, false)->fault);
    assert(!hidppReading(Report(hidpp).first(7), true)); // Unified Battery needs its external power byte.
    hidpp[4] = 0;
    hidpp[6] = 2;
    assert(!hidppReading(hidpp, false));
    assert(hidppReading(hidpp, true)->level == 0 && hidppReading(hidpp, true)->charging);
    hidpp[4] = 45;
    hidpp[6] = 0xff;
    assert(hidppReading(hidpp, true)->level == 45 && hidppReading(hidpp, true)->fault);
    assert(std::wstring_view(hidppDeviceKind(3)) == L"鼠标");
    assert(std::wstring_view(hidppDeviceKind(5)) == L"轨迹球");
    assert(std::wstring_view(hidppDeviceKind(0xff)) == L"外设");

    assert(needsHidppConfirmation(4, 90));
    assert(needsHidppConfirmation(4, 20));
    assert(!needsHidppConfirmation(89, 90));
    assert(!needsHidppConfirmation(4, 5));
    assert(!confirmHidppReading(4, std::nullopt, 90));
    assert(!confirmHidppReading(4, Reading{90}, std::nullopt));
    assert(!confirmHidppReading(4, Reading{50}, 90));
    const auto corrected = confirmHidppReading(4, Reading{90}, 90);
    assert(corrected && corrected->level == 90);
    const auto confirmedLow = confirmHidppReading(4, Reading{4}, 90);
    assert(confirmedLow && confirmedLow->level == 4);

    std::array<std::uint8_t, 7> vt{0xbb, 0xb0, 0, 0, 0, 2, 0x7f};
    const auto unknownWhileCharging = rapooReading(vt, 7);
    assert(unknownWhileCharging && unknownWhileCharging->charging && !unknownWhileCharging->level);
    assert(!rapooReading(vt, 16));
    vt[6] = 101;
    assert(!rapooReading(vt, 7));
    std::array<std::uint8_t, 13> vt0{7, 0x30, 0, 0, 0, 0, 0, 2, 0};
    assert(rapooReading(vt0, 13)->level == 0 && rapooReading(vt0, 13)->charging);
    std::array<std::uint8_t, 16> vt2{7, 0x30, 0, 0, 0, 0, 0, 1, 34};
    assert(rapooReading(vt2, 16)->level == 34 && !rapooReading(vt2, 16)->charging);
    assert(!rapooReading(vt2, 19));
    ++vt2[1];
    assert(!rapooReading(vt2, 16));
    std::array<std::uint8_t, 19> vt19{7, 0, 0, 0, 0, 0, 0, 1, 100};
    assert(rapooReading(vt19, 19)->level == 100 && !rapooReading(vt19, 19)->charging);

    const auto before = std::chrono::system_clock::time_point{std::chrono::seconds{123}};
    const auto after = before + std::chrono::seconds{60};
    Device keyboard;
    keyboard.present = true;
    keyboard.acceptReading(Reading{68}, before);
    keyboard.acceptReading(*compxReading(compx), after);
    assert(keyboard.fresh && !keyboard.displayLevel() && keyboard.updated == before);
    keyboard.present = false;
    assert(keyboard.displayLevel() == 68);
    Device mouse;
    mouse.present = true;
    mouse.acceptReading(Reading{34}, before);
    mouse.acceptReading(*unknownWhileCharging, after);
    assert(mouse.fresh && mouse.reading.charging && !mouse.displayLevel() && mouse.updated == after);

    std::array<std::uint8_t, 91> request{}, reply{};
    request[2] = 0x1f;
    request[6] = 2;
    request[7] = 7;
    request[8] = 0x80;
    reply = request;
    reply[1] = 2;
    reply[10] = 128;
    reply[89] = razerChecksum(reply);
    assert(razerValue(reply, request) == 128);
    reply[2] = 0x3f;
    assert(!razerValue(reply, request));
    std::array<std::uint8_t, 65> other{0, 0xa1, 0, 0, 2, 0, 0x83, 1, 64};
    assert(sinowealthReading(other)->level == 64 && sinowealthReading(other)->charging);
    other[6] = 0x84;
    assert(!sinowealthReading(other));
    std::cout << "Compx, Logitech HID++, Rapoo, Razer and Sinowealth protocol checks passed.\n";
}
