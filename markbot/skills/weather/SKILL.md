---
name: weather
description: Get current weather and forecasts (no API key required).
homepage: https://wttr.in/:help
metadata: {"markbot":{"emoji":"🌤️","requires":{"bins":["curl"]}}}
---

# Weather

Two free services, no API keys needed.

## wttr.in (primary)

Quick one-liner:
```bash
curl -s "wttr.in/London?format=3"
# Output: London: ⛅️ +8°C
```

Compact format:
```bash
curl -s "wttr.in/London?format=%l:+%c+%t+%h+%w"
# Output: London: ⛅️ +8°C 71% ↙5km/h
```

Full forecast:
```bash
curl -s "wttr.in/London?T"
```

Format codes: `%c` condition · `%t` temp · `%h` humidity · `%w` wind · `%l` location · `%m` moon

Tips:
- URL-encode spaces: `wttr.in/New+York`
- Airport codes: `wttr.in/JFK`
- Units: `?m` (metric) `?u` (USCS)
- Today only: `?1` · Current only: `?0`
- PNG: `curl -s "wttr.in/Berlin.png" -o /tmp/weather.png`

## Open-Meteo (fallback, JSON) ⭐ 推荐

Free, no key, good for programmatic use:
```bash
# 当前天气
curl -s "https://api.open-meteo.com/v1/forecast?latitude=31.23&longitude=121.47&current_weather=true"

# 未来天气预报
curl -s "https://api.open-meteo.com/v1/forecast?latitude=31.23&longitude=121.47&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=Asia/Shanghai&forecast_days=5"
```

### 常用城市坐标
| 城市 | latitude | longitude |
|------|----------|-----------|
| 上海 | 31.23 | 121.47 |
| 北京 | 39.91 | 116.39 |
| 广州 | 23.13 | 113.26 |
| 深圳 | 22.54 | 114.06 |
| 成都 | 30.67 | 104.07 |
| 杭州 | 30.27 | 120.15 |

### 常用参数
| 参数 | 说明 |
|------|------|
| `daily=weathercode` | 天气代码 |
| `daily=temperature_2m_max/min` | 最高/最低温度 |
| `daily=precipitation_sum` | 降水量 |
| `daily=windspeed_10m_max` | 最大风速 |
| `timezone=Asia/Shanghai` | 时区 |
| `forecast_days=5` | 预报天数 (1-16) |

### 天气代码对照
| 代码 | 含义 |
|------|------|
| 0 | 晴天 ☀️ |
| 1-3 | 多云 ⛅ |
| 45-48 | 雾 🌫️ |
| 51-67 | 雨 🌧️ |
| 71-77 | 雪 ❄️ |
| 80-82 | 阵雨 🌦️ |
| 95-99 | 雷暴 ⛈️ |

Docs: https://open-meteo.com/en/docs
