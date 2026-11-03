"""weather 工具测试：解析、中文映射与错误处理。

统一通过公开的 :func:`get_weather` 驱动，用 ``httpx.MockTransport`` 替掉网络，
避免测试触碰模块私有实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import blue_service_weather as weather
import httpx
import pytest
from blue_service_weather import WeatherConfig, get_weather

SAMPLE_PAYLOAD: dict[str, Any] = {
    "current_condition": [
        {
            "temp_C": "8",
            "FeelsLikeC": "6",
            "humidity": "45",
            "windspeedKmph": "12",
            "winddir16Point": "NW",
            "pressure": "1021",
            "visibility": "10",
            "uvIndex": "3",
            "observation_time": "11:26 AM",
            "weatherCode": "113",
            "weatherDesc": [{"value": "Sunny"}],
        }
    ],
    "nearest_area": [
        {
            "areaName": [{"value": "Beijing"}],
            "region": [{"value": "Beijing"}],
            "country": [{"value": "China"}],
        }
    ],
    "request": [{"query": "Beijing"}],
    "weather": [
        {
            "date": "2026-11-05",
            "maxtempC": "12",
            "mintempC": "2",
            "avgtempC": "7",
            "totalSnow_cm": "0.0",
            "uvIndex": "4",
            "astronomy": [{"sunrise": "06:46 AM", "sunset": "05:09 PM"}],
            "hourly": [
                {
                    "weatherCode": "113",
                    "weatherDesc": [{"value": "Sunny"}],
                    "chanceofrain": "0",
                },
                {
                    "weatherCode": "116",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                    "chanceofrain": "10",
                },
                {
                    "weatherCode": "116",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                    "chanceofrain": "20",
                },
            ],
        },
        {
            "date": "2026-11-06",
            "maxtempC": "10",
            "mintempC": "1",
            "avgtempC": "5",
            "totalSnow_cm": "0.0",
            "uvIndex": "3",
            "astronomy": [{"sunrise": "06:47 AM", "sunset": "05:08 PM"}],
            "hourly": [
                {
                    "weatherCode": "296",
                    "weatherDesc": [{"value": "Light rain"}],
                    "chanceofrain": "60",
                }
            ],
        },
        {
            "date": "2026-11-07",
            "maxtempC": "9",
            "mintempC": "0",
            "avgtempC": "4",
            "totalSnow_cm": "0.0",
            "uvIndex": "2",
            "astronomy": [{"sunrise": "06:48 AM", "sunset": "05:07 PM"}],
            "hourly": [
                {
                    "weatherCode": "230",
                    "weatherDesc": [{"value": "Blizzard"}],
                    "chanceofrain": "80",
                }
            ],
        },
    ],
}

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """configure() 的进程级配置不应泄漏到其它用例。"""
    monkeypatch.setattr(weather, "_config", WeatherConfig())


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """把 httpx.AsyncClient 换成带 MockTransport 的版本。"""
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(weather.httpx, "AsyncClient", factory)


def _json_handler(payload: object, seen: dict[str, Any] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json=payload)

    return handler


def test_get_weather_parses_current_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    """当前实况字段被正确降维，天气码翻译为中文。"""
    _patch_client(monkeypatch, _json_handler(SAMPLE_PAYLOAD))

    result = asyncio.run(get_weather("Beijing", days=3))

    assert result.location == "Beijing"
    assert result.region == "Beijing"
    assert result.country == "China"
    assert result.current.temperature_c == pytest.approx(8.0)
    assert result.current.feels_like_c == pytest.approx(6.0)
    assert result.current.description == "晴"
    assert result.current.humidity_percent == 45
    assert result.current.wind_direction == "NW"
    assert result.current.pressure_hpa == pytest.approx(1021.0)
    assert result.current.uv_index == 3
    assert result.current.observed_at == "11:26 AM"


def test_get_weather_maps_wttr_extended_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """wttr.in 的扩展码（如 Smoke / Smoky haze）也要翻译。"""
    payload = {
        "current_condition": [
            {"weatherCode": "146", "weatherDesc": [{"value": "Smoke"}]}
        ],
        "nearest_area": [{"areaName": [{"value": "Beijing"}]}],
        "weather": [],
    }
    _patch_client(monkeypatch, _json_handler(payload))

    assert asyncio.run(get_weather("Beijing")).current.description == "烟"


def test_get_weather_parses_forecast(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐日预报取主导天气与最大降雨概率。"""
    _patch_client(monkeypatch, _json_handler(SAMPLE_PAYLOAD))

    result = asyncio.run(get_weather("Beijing", days=3))

    assert len(result.forecast) == 3
    first = result.forecast[0]
    assert first.date == "2026-11-05"
    assert first.description == "多云", "116 出现两次，应为主导天气"
    assert first.chance_of_rain_percent == 20
    assert first.max_temp_c == pytest.approx(12.0)
    assert first.sunrise == "06:46 AM"
    assert result.forecast[1].description == "小雨"
    assert result.forecast[2].description == "暴风雪"


def test_get_weather_respects_day_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """days 只截取请求的天数。"""
    _patch_client(monkeypatch, _json_handler(SAMPLE_PAYLOAD))

    assert len(asyncio.run(get_weather("Beijing", days=1)).forecast) == 1
    assert len(asyncio.run(get_weather("Beijing", days=2)).forecast) == 2


def test_get_weather_encodes_location_and_identifies_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文地名应被 URL 编码，并带自有 UA。"""
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _json_handler(SAMPLE_PAYLOAD, seen))

    asyncio.run(get_weather("北京"))

    assert "%E5%8C%97%E4%BA%AC" in seen["url"]
    assert "format=j1" in seen["url"]
    assert seen["ua"] == weather.USER_AGENT


def test_get_weather_falls_back_to_english_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知天气码回退为上游英文描述。"""
    payload = {
        "current_condition": [
            {"weatherCode": "999", "weatherDesc": [{"value": "Weird"}]}
        ],
        "nearest_area": [{"areaName": [{"value": "Nowhere"}]}],
        "weather": [],
    }
    _patch_client(monkeypatch, _json_handler(payload))

    result = asyncio.run(get_weather("Nowhere"))

    assert result.current.description == "Weird"
    assert result.forecast == []


def test_get_weather_tolerates_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字段缺失时不抛异常，退化为默认值。"""
    _patch_client(monkeypatch, _json_handler({}))

    result = asyncio.run(get_weather("Nowhere"))

    assert result.current.temperature_c == pytest.approx(0.0)
    assert result.current.description == "未知天气"
    assert result.current.wind_direction == "未知"
    assert result.forecast == []


def test_get_weather_rejects_empty_location() -> None:
    """空地名直接报错。"""
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(get_weather("   "))


def test_get_weather_rejects_out_of_range_days() -> None:
    """预报天数越界直接报错。"""
    with pytest.raises(ValueError, match="days"):
        asyncio.run(get_weather("Beijing", days=0))
    with pytest.raises(ValueError, match="days"):
        asyncio.run(get_weather("Beijing", days=weather.MAX_FORECAST_DAYS + 1))


def test_get_weather_reports_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游非 2xx 时给出可读错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)

    with pytest.raises(ValueError, match="HTTP 404"):
        asyncio.run(get_weather("Nowhereville"))


def test_get_weather_reports_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接失败时给出可读错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _patch_client(monkeypatch, handler)

    with pytest.raises(ValueError, match="访问天气服务失败"):
        asyncio.run(get_weather("Beijing"))


def test_configure_applies_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """configure() 后请求会使用新的 base_url。"""
    weather.configure(WeatherConfig(base_url="https://example.test/", timeout=3.0))
    seen: dict[str, Any] = {}
    _patch_client(monkeypatch, _json_handler(SAMPLE_PAYLOAD, seen))

    asyncio.run(get_weather("Beijing"))

    assert seen["url"].startswith("https://example.test/Beijing")


def test_weather_code_table_is_wellformed() -> None:
    """中文映射表的键都是 WWO 三位码。"""
    assert weather.WEATHER_CODE_ZH
    assert all(code.isdigit() and len(code) == 3 for code in weather.WEATHER_CODE_ZH)
    assert all(value for value in weather.WEATHER_CODE_ZH.values())


def test_config_model_is_exported() -> None:
    """契约测试依赖的 CONFIG_MODEL 已导出。"""
    assert weather.CONFIG_MODEL is WeatherConfig
