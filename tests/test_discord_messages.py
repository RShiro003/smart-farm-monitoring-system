"""Discord payload tests without real network calls or operational databases."""
import json
import unittest
import urllib.error
from unittest import mock

from app.services import discord_alert_service as discord


class DiscordMessageTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/test-id/test-token"
    BODY = (
        "장치: esp32_01\n기간: 2026-09-28 ~ 2026-10-04 23:59\n"
        "수집: 100건 · 센서 오류 포함 0건\n"
        "온도: 평균 24°C (최저 20 / 최고 28°C)\n"
        "알림: 이상 1건(위험 0건) · 복구 1건 · 오프라인 0건"
    )

    def setUp(self):
        patcher = mock.patch.object(discord.urllib.request, "urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.urlopen.return_value.__enter__.return_value.status = 204

    def test_daily_and_weekly_cards_have_distinct_colors_and_explicit_titles(self):
        for label, title, color in (
            ("일간", "🟦 스마트팜 일간 요약 · 1일", 0x3498DB),
            ("주간", "🟪 스마트팜 주간 요약 · 7일", 0x9B59B6),
        ):
            with self.subTest(label=label):
                self.assertTrue(discord.send_discord_message(
                    f"[스마트팜 {label} 요약]\n{self.BODY}", self.URL,
                ))
                request = self.urlopen.call_args.args[0]
                self.assertEqual(request.full_url, self.URL)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 5})
                self.assertEqual(json.loads(request.data), {"allowed_mentions": {"parse": []}, "embeds": [{
                    "title": title, "description": self.BODY, "color": color,
                }]})

    def test_ordinary_alerts_remain_plain_text(self):
        for message in ("[스마트팜 이상] 온도 40°C", "[스마트팜 복구] 정상",
                        "[스마트팜 장치 무응답] esp32_01", "연결 테스트",
                        "알림 본문의 [스마트팜 일간 요약]\n설명"):
            with self.subTest(message=message):
                self.assertTrue(discord.send_discord_message(message, self.URL))
                request = self.urlopen.call_args.args[0]
                self.assertEqual(json.loads(request.data), {"content": message, "allowed_mentions": {"parse": []}})

    def test_incomplete_or_unknown_summary_preserves_text(self):
        for message in ("[스마트팜 일간 요약]", "[스마트팜 주간 요약]\n",
                        "[스마트팜 월간 요약]\n본문"):
            with self.subTest(message=message):
                self.assertEqual(discord._message_payload(message), {"content": message})

    def test_description_limit_preserves_text_without_truncation(self):
        heading = "[스마트팜 주간 요약]\n"
        body = "가" * 4096
        self.assertEqual(discord._message_payload(heading + body)["embeds"][0]["description"], body)
        parts = discord.split_discord_message(heading + body + "나")
        self.assertEqual(parts, [heading + body, heading + "나"])

    def test_large_summaries_are_sent_without_loss_and_keep_color(self):
        for label, color in (("일간", 0x3498DB), ("주간", 0x9B59B6)):
            with self.subTest(label=label):
                self.urlopen.reset_mock()
                body = ("측정 🌱\n" * 2000) + "마지막 줄"
                self.assertTrue(discord.send_discord_message(
                    f"[스마트팜 {label} 요약]\n{body}", self.URL,
                ))
                embeds = [json.loads(call.args[0].data)["embeds"][0]
                          for call in self.urlopen.call_args_list]
                self.assertGreater(len(embeds), 1)
                self.assertEqual("".join(item["description"] for item in embeds), body)
                for item in embeds:
                    self.assertEqual(item["color"], color)
                    self.assertLessEqual(len(item["description"].encode("utf-16-le")) // 2, 4096)
                    self.assertLessEqual(len(item["title"]), 256)

    def test_long_plain_text_is_split_at_2000_units(self):
        message = "🌱" * 2001 + "끝"
        self.assertTrue(discord.send_discord_message(message, self.URL))
        parts = [json.loads(call.args[0].data)["content"]
                 for call in self.urlopen.call_args_list]
        self.assertEqual("".join(parts), message)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 2000 for part in parts))

    def test_splitting_is_idempotent_including_blank_description_parts(self):
        message = "[스마트팜 주간 요약]\n" + " " * 4096 + "끝"
        parts = discord.split_discord_message(message)
        for part in parts:
            self.assertEqual(discord.split_discord_message(part), [part])
            payload = discord._message_payload(part)
            self.assertIn("embeds", payload)
            self.assertTrue(payload["embeds"][0]["title"])

    def test_exact_plain_text_boundary_stays_one_request(self):
        message = "가" * 2000
        self.assertTrue(discord.send_discord_message(message, self.URL))
        self.urlopen.assert_called_once()
        self.assertEqual(json.loads(self.urlopen.call_args.args[0].data), {"content": message, "allowed_mentions": {"parse": []}})

    def test_partial_direct_delivery_failure_returns_false_and_stops(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 204
        self.urlopen.side_effect = [response, urllib.error.URLError("offline")]
        self.assertFalse(discord.send_discord_message(
            "[스마트팜 주간 요약]\n" + "가" * 9000, self.URL,
        ))
        self.assertEqual(self.urlopen.call_count, 2)

    def test_missing_sensor_values_remain_visible(self):
        body = "장치: esp32_01\n온도: 데이터 없음\n조도: 데이터 없음"
        payload = discord._message_payload("[스마트팜 일간 요약]\n" + body)
        self.assertEqual(payload["embeds"][0]["description"], body)

    def test_network_failures_still_return_false_for_retry(self):
        for error in (
            urllib.error.HTTPError(self.URL, 429, "rate limited", {}, None),
            urllib.error.URLError("offline"), TimeoutError("timeout"),
        ):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                self.assertFalse(discord.send_discord_message(
                    "[스마트팜 주간 요약]\n" + self.BODY, self.URL,
                ))

    def test_missing_or_untrusted_webhook_never_sends(self):
        with mock.patch.dict(discord.os.environ, {"DISCORD_WEBHOOK_URL": ""}):
            for url in (None, "https://example.com/api/webhooks/test"):
                self.assertFalse(discord.send_discord_message("test", url))
        self.urlopen.assert_not_called()

    def test_environment_webhook_is_used_when_no_override(self):
        with mock.patch.dict(discord.os.environ, {"DISCORD_WEBHOOK_URL": self.URL}):
            self.assertTrue(discord.send_discord_message("test"))
        self.assertEqual(self.urlopen.call_args.args[0].full_url, self.URL)
