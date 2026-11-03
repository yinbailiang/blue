"""blue 外置工具：查询指定地点的天气与短期预报。

数据来自 `wttr.in <https://wttr.in>`_（免费、无需密钥）。上游对默认
User-Agent 会限流，因此显式声明 UA；天气描述按 WWO 天气码本地映射为中文，
不依赖上游的 ``lang_zh`` 翻译（该字段对不少天气码会原样返回英文）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from blue.runtime import load_runtime_config, run_tool

logger = logging.getLogger(__name__)

USER_AGENT = "blue-weather-tool/0.1"
"""上游按 User-Agent 限流，默认 UA 容易被拒。"""

MAX_FORECAST_DAYS = 3
"""wttr.in 的 j1 接口最多返回 3 天预报。"""

WEATHER_CODE_ZH: dict[str, str] = {
    "113": "晴",
    "116": "多云",
    "119": "阴",
    "122": "阴天",
    "143": "薄雾",
    "146": "烟",
    "149": "烟霾",
    "176": "局部有雨",
    "179": "局部有雪",
    "182": "局部雨夹雪",
    "185": "局部冻雨",
    "200": "雷暴",
    "227": "风吹雪",
    "230": "暴风雪",
    "248": "雾",
    "260": "冻雾",
    "263": "零星毛毛雨",
    "266": "毛毛雨",
    "281": "冻毛毛雨",
    "284": "强冻毛毛雨",
    "293": "零星小雨",
    "296": "小雨",
    "299": "中雨",
    "302": "大雨",
    "305": "间歇性大雨",
    "308": "暴雨",
    "311": "小冻雨",
    "314": "冻雨",
    "317": "小雨夹雪",
    "320": "雨夹雪",
    "323": "零星小雪",
    "326": "小雪",
    "329": "局部中雪",
    "332": "中雪",
    "335": "局部大雪",
    "338": "大雪",
    "350": "冰粒",
    "353": "小阵雨",
    "356": "中阵雨",
    "359": "强阵雨",
    "362": "小冻雨夹雪",
    "365": "冻雨夹雪",
    "368": "小阵雪",
    "371": "中到大阵雪",
    "374": "小冰粒",
    "377": "中到大冰粒",
    "386": "雷阵雨",
    "389": "强雷阵雨",
    "392": "雷雪",
    "395": "强降雪",
}
"""WWO 天气码 → 中文描述。"""


class WeatherConfig(BaseModel):
    """weather 服务的运行时配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(default="https://wttr.in", description="天气接口地址")
    lang: str = Field(default="zh", description="传给上游的语言参数")
    timeout: float = Field(default=15.0, gt=0, description="请求超时（秒）")


class WeatherNow(BaseModel):
    """当前实况。"""

    temperature_c: float = Field(description="气温（摄氏度）")
    feels_like_c: float = Field(description="体感温度（摄氏度）")
    description: str = Field(description="天气描述")
    humidity_percent: int = Field(description="相对湿度（%）")
    wind_kmph: float = Field(description="风速（km/h）")
    wind_direction: str = Field(description="风向（16 方位）")
    pressure_hpa: float = Field(description="气压（hPa）")
    visibility_km: float = Field(description="能见度（km）")
    uv_index: int = Field(description="紫外线指数")
    observed_at: str = Field(description="观测时间（上游原始格式）")


class WeatherDay(BaseModel):
    """单日预报。"""

    date: str = Field(description="日期（YYYY-MM-DD）")
    description: str = Field(description="当日主导天气")
    max_temp_c: float = Field(description="最高气温（摄氏度）")
    min_temp_c: float = Field(description="最低气温（摄氏度）")
    avg_temp_c: float = Field(description="平均气温（摄氏度）")
    chance_of_rain_percent: int = Field(description="当日最高降雨概率（%）")
    total_snow_cm: float = Field(description="累计降雪（cm）")
    uv_index: int = Field(description="紫外线指数")
    sunrise: str = Field(description="日出时间")
    sunset: str = Field(description="日落时间")


class WeatherResult(BaseModel):
    """天气查询结果。"""

    location: str = Field(description="查询地点")
    region: str = Field(description="上游解析到的地区名")
    country: str = Field(description="国家/地区")
    current: WeatherNow = Field(description="当前实况")
    forecast: list[WeatherDay] = Field(
        default_factory=list[WeatherDay], description="逐日预报"
    )


_config: WeatherConfig = WeatherConfig()
"""进程级配置；由 :func:`configure` 在启动时注入。"""


def configure(config: WeatherConfig) -> None:
    """设置本进程后续查询使用的配置。"""
    global _config
    _config = config


async def get_weather(
    location: Annotated[str, "城市或地名，例如 北京、Shanghai、Tokyo"],
    days: Annotated[int, Field(ge=1, le=MAX_FORECAST_DAYS), "预报天数"] = 1,
) -> WeatherResult:
    """查询指定地点的实时天气与未来数日预报。"""
    query = location.strip()
    if not query:
        raise ValueError("location 不能为空")
    if not 1 <= days <= MAX_FORECAST_DAYS:
        raise ValueError(f"days 必须在 1 到 {MAX_FORECAST_DAYS} 之间")

    payload = await _fetch(query)
    return _build_result(payload, days=days)


async def _fetch(location: str) -> dict[str, Any]:
    """请求上游并返回解析后的 JSON 对象。"""
    url = f"{_config.base_url.rstrip('/')}/{quote(location)}"
    try:
        async with httpx.AsyncClient(timeout=_config.timeout) as client:
            response = await client.get(
                url,
                params={"format": "j1", "lang": _config.lang},
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            data: object = response.json()
    except httpx.HTTPStatusError as error:
        raise ValueError(
            f"天气服务返回 HTTP {error.response.status_code}，请检查地名是否正确"
        ) from error
    except httpx.HTTPError as error:
        raise ValueError(f"访问天气服务失败：{error}") from error
    except ValueError as error:
        raise ValueError(f"天气服务返回了无法解析的内容：{error}") from error
    if not isinstance(data, dict):
        raise ValueError("天气服务返回了非预期的结构")
    return cast(dict[str, Any], data)


def _build_result(payload: dict[str, Any], *, days: int) -> WeatherResult:
    """把上游 JSON 降维成稳定的展示模型。"""
    nearest = _first_dict(payload.get("nearest_area"))
    current = _first_dict(payload.get("current_condition"))

    now = WeatherNow(
        temperature_c=_as_float(current.get("temp_C")),
        feels_like_c=_as_float(current.get("FeelsLikeC")),
        description=_describe(
            _as_str(current.get("weatherCode")),
            _nested_value(current.get("weatherDesc")),
        ),
        humidity_percent=_as_int(current.get("humidity")),
        wind_kmph=_as_float(current.get("windspeedKmph")),
        wind_direction=_as_str(current.get("winddir16Point"), "未知"),
        pressure_hpa=_as_float(current.get("pressure")),
        visibility_km=_as_float(current.get("visibility")),
        uv_index=_as_int(current.get("uvIndex")),
        observed_at=_as_str(current.get("observation_time")),
    )

    forecast: list[WeatherDay] = []
    for item in _dict_list(payload.get("weather"))[:days]:
        hourly = _dict_list(item.get("hourly"))
        astronomy = _first_dict(item.get("astronomy"))
        forecast.append(
            WeatherDay(
                date=_as_str(item.get("date")),
                description=_dominant_description(hourly),
                max_temp_c=_as_float(item.get("maxtempC")),
                min_temp_c=_as_float(item.get("mintempC")),
                avg_temp_c=_as_float(item.get("avgtempC")),
                chance_of_rain_percent=max(
                    (_as_int(hour.get("chanceofrain")) for hour in hourly),
                    default=0,
                ),
                total_snow_cm=_as_float(item.get("totalSnow_cm")),
                uv_index=_as_int(item.get("uvIndex")),
                sunrise=_as_str(astronomy.get("sunrise"), "未知"),
                sunset=_as_str(astronomy.get("sunset"), "未知"),
            )
        )

    return WeatherResult(
        location=_nested_value(nearest.get("areaName"))
        or _nested_value(payload.get("request")),
        region=_nested_value(nearest.get("region")),
        country=_nested_value(nearest.get("country")),
        current=now,
        forecast=forecast,
    )


def _dominant_description(hourly: list[dict[str, Any]]) -> str:
    """取当日出现次数最多的天气码作为这一天的主导天气。"""
    counts: dict[str, int] = {}
    english: dict[str, str] = {}
    for entry in hourly:
        code = _as_str(entry.get("weatherCode"))
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
        english.setdefault(code, _nested_value(entry.get("weatherDesc")))
    if not counts:
        return "未知"
    code = max(counts, key=lambda item: counts[item])
    return _describe(code, english.get(code, ""))


def _describe(code: str, fallback: str) -> str:
    """天气码优先映射为中文，未知码回退上游英文描述。"""
    return WEATHER_CODE_ZH.get(code) or fallback.strip() or "未知天气"


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _first_dict(value: object) -> dict[str, Any]:
    """上游大量字段是「单元素数组包对象」，这里取其第一个元素。"""
    if not isinstance(value, list):
        return {}
    items = cast(list[object], value)
    if not items:
        return {}
    first = items[0]
    if isinstance(first, dict):
        return cast(dict[str, Any], first)
    return {}


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def _nested_value(value: object) -> str:
    """形如 ``[{"value": "Beijing"}]`` 的字段取其中的 value。"""
    return _as_str(_first_dict(value).get("value"))


__all__ = [
    "CONFIG_MODEL",
    "MAX_FORECAST_DAYS",
    "WEATHER_CODE_ZH",
    "WeatherConfig",
    "WeatherDay",
    "WeatherNow",
    "WeatherResult",
    "configure",
    "get_weather",
    "main",
]

CONFIG_MODEL = WeatherConfig
"""契约测试使用的运行时配置模型。"""

TOOL = get_weather
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    configure(load_runtime_config(WeatherConfig))
    return run_tool(TOOL, "weather")


if __name__ == "__main__":
    raise SystemExit(main())
