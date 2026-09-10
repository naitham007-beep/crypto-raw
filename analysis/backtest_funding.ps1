# backtest_funding.ps1  (T019 rail B - evidence layer)
# Question: when funding is extreme vs its own recent history, what happens to price next?
# Output: analysis/backtest_funding_<SYM>.json  (consumed by the web page as "what this condition meant historically")
# ASCII only on purpose - PowerShell 5.1 mangles Thai unless the file is UTF-8 BOM.

param(
    [string[]]$Symbols = @("BTC", "ETH"),
    [int]$Pages = 12,          # 100 funding rounds per page, 3 rounds/day -> 12 pages ~ 400 days
    [int]$LookbackRounds = 90, # percentile window = 30 days (90 funding rounds)
    [string]$OutDir = "$PSScriptRoot"
)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "Stop"

function Get-FundingHistory([string]$instId, [int]$pages) {
    $all = @()
    $after = $null
    for ($i = 0; $i -lt $pages; $i++) {
        $url = "https://www.okx.com/api/v5/public/funding-rate-history?instId=$instId&limit=100"
        if ($after) { $url += "&after=$after" }
        $r = Invoke-RestMethod -Uri $url -TimeoutSec 30
        if ($r.code -ne "0" -or $r.data.Count -eq 0) { break }
        $all += $r.data
        $after = $r.data[-1].fundingTime
        Start-Sleep -Milliseconds 250
    }
    # oldest -> newest
    $all | Sort-Object { [long]$_.fundingTime }
}

function Get-Klines([string]$symbol, [long]$startMs) {
    # Binance 1h klines, paged 1000 at a time. data-api mirror works from any region.
    $out = @{}
    $cur = $startMs
    $nowMs = [long]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    while ($cur -lt $nowMs) {
        $url = "https://data-api.binance.vision/api/v3/klines?symbol=${symbol}USDT&interval=1h&limit=1000&startTime=$cur"
        $r = Invoke-RestMethod -Uri $url -TimeoutSec 30
        if (-not $r -or $r.Count -eq 0) { break }
        foreach ($k in $r) { $out[[long]$k[0]] = [double]$k[4] }   # openTime -> close
        $last = [long]$r[-1][0]
        if ($last -le $cur) { break }
        $cur = $last + 3600000
        Start-Sleep -Milliseconds 150
    }
    $out
}

function Percentile-Rank([double[]]$window, [double]$v) {
    if ($window.Count -eq 0) { return [double]::NaN }
    $below = 0
    foreach ($w in $window) { if ($w -lt $v) { $below++ } }
    100.0 * $below / $window.Count
}

function Summarize($rows, [string]$label) {
    $n = $rows.Count
    if ($n -eq 0) { return [ordered]@{ label = $label; n = 0 } }
    $r8 = $rows | ForEach-Object { $_.ret8 } | Where-Object { $_ -ne $null }
    $r24 = $rows | ForEach-Object { $_.ret24 } | Where-Object { $_ -ne $null }
    $up8 = ($r8 | Where-Object { $_ -gt 0 }).Count
    $up24 = ($r24 | Where-Object { $_ -gt 0 }).Count
    $s8 = ($r8 | Measure-Object -Average).Average
    $s24 = ($r24 | Measure-Object -Average).Average
    $m8 = if ($r8.Count) { ($r8 | Sort-Object)[[int]($r8.Count / 2)] } else { $null }
    $m24 = if ($r24.Count) { ($r24 | Sort-Object)[[int]($r24.Count / 2)] } else { $null }
    [ordered]@{
        label      = $label
        n          = $n
        n8         = $r8.Count
        n24        = $r24.Count
        up_rate_8h = if ($r8.Count) { [math]::Round(100.0 * $up8 / $r8.Count, 1) } else { $null }
        up_rate_24h= if ($r24.Count) { [math]::Round(100.0 * $up24 / $r24.Count, 1) } else { $null }
        avg_ret_8h = if ($s8 -ne $null) { [math]::Round($s8, 3) } else { $null }
        avg_ret_24h= if ($s24 -ne $null) { [math]::Round($s24, 3) } else { $null }
        med_ret_8h = if ($m8 -ne $null) { [math]::Round($m8, 3) } else { $null }
        med_ret_24h= if ($m24 -ne $null) { [math]::Round($m24, 3) } else { $null }
    }
}

foreach ($sym in $Symbols) {
    Write-Host "=== $sym ===" -ForegroundColor Cyan
    $fund = Get-FundingHistory "$sym-USDT-SWAP" $Pages
    Write-Host ("funding rounds: {0}  ({1} -> {2})" -f $fund.Count,
        ([DateTimeOffset]::FromUnixTimeMilliseconds([long]$fund[0].fundingTime).UtcDateTime.ToString("yyyy-MM-dd")),
        ([DateTimeOffset]::FromUnixTimeMilliseconds([long]$fund[-1].fundingTime).UtcDateTime.ToString("yyyy-MM-dd")))

    $startMs = [long]$fund[0].fundingTime - 3600000
    $kl = Get-Klines $sym $startMs
    Write-Host ("klines 1h: {0}" -f $kl.Count)

    # build rows: at each funding settlement, percentile of that rate vs previous N rounds,
    # then forward price return over 8h / 24h measured from the hourly close at settlement time.
    $rows = @()
    $rates = @()
    foreach ($f in $fund) {
        $t = [long]$f.fundingTime
        $rate = [double]$f.fundingRate
        if ($rates.Count -ge $LookbackRounds) {
            $win = $rates[($rates.Count - $LookbackRounds)..($rates.Count - 1)]
            $p = Percentile-Rank $win $rate
            $hour = $t - ($t % 3600000)
            $p0 = $kl[$hour]
            $p8 = $kl[($hour + 8 * 3600000)]
            $p24 = $kl[($hour + 24 * 3600000)]
            if ($p0) {
                $rows += [pscustomobject]@{
                    t     = $t
                    rate  = $rate
                    pct   = [math]::Round($p, 1)
                    ret8  = if ($p8) { 100.0 * ($p8 - $p0) / $p0 } else { $null }
                    ret24 = if ($p24) { 100.0 * ($p24 - $p0) / $p0 } else { $null }
                }
            }
        }
        $rates += $rate
    }
    Write-Host ("usable observations: {0}" -f $rows.Count)

    $buckets = [ordered]@{
        "hot_p80plus"   = ($rows | Where-Object { $_.pct -ge 80 })
        "warm_p60_80"   = ($rows | Where-Object { $_.pct -ge 60 -and $_.pct -lt 80 })
        "mid_p40_60"    = ($rows | Where-Object { $_.pct -ge 40 -and $_.pct -lt 60 })
        "cool_p20_40"   = ($rows | Where-Object { $_.pct -ge 20 -and $_.pct -lt 40 })
        "cold_p20minus" = ($rows | Where-Object { $_.pct -lt 20 })
    }

    $result = [ordered]@{
        symbol           = $sym
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("s") + "Z"
        method           = "funding percentile vs previous $LookbackRounds rounds (30d); forward close-to-close return from Binance 1h klines"
        rounds           = $fund.Count
        observations     = $rows.Count
        from_utc         = ([DateTimeOffset]::FromUnixTimeMilliseconds([long]$fund[0].fundingTime).UtcDateTime.ToString("s") + "Z")
        to_utc           = ([DateTimeOffset]::FromUnixTimeMilliseconds([long]$fund[-1].fundingTime).UtcDateTime.ToString("s") + "Z")
        buckets          = @()
        baseline         = (Summarize $rows "all_observations")
    }
    foreach ($k in $buckets.Keys) { $result.buckets += (Summarize $buckets[$k] $k) }

    # stability check: does the pattern hold in BOTH halves of the sample, or is it noise?
    $mid = [int]($rows.Count / 2)
    $firstHalf = $rows[0..($mid - 1)]
    $secondHalf = $rows[$mid..($rows.Count - 1)]
    $result.stability = @()
    foreach ($k in $buckets.Keys) {
        $a = Summarize ($firstHalf | Where-Object { $buckets[$k] -contains $_ }) "$k/first_half"
        $b = Summarize ($secondHalf | Where-Object { $buckets[$k] -contains $_ }) "$k/second_half"
        $result.stability += $a
        $result.stability += $b
    }
    Write-Host "-- stability (same bucket, two halves of the sample) --" -ForegroundColor DarkGray
    foreach ($s in $result.stability) {
        Write-Host ("  {0,-30} n={1,3}  up8h={2,5}%  avg8h={3,7}%" -f $s.label, $s.n, $s.up_rate_8h, $s.avg_ret_8h)
    }
    $result.rows = $rows   # keep the raw observations so other questions can be asked later without refetching

    foreach ($b in $result.buckets) {
        Write-Host ("{0,-14} n={1,4}  up8h={2,5}%  up24h={3,5}%  avg8h={4,7}%  avg24h={5,7}%" -f `
            $b.label, $b.n, $b.up_rate_8h, $b.up_rate_24h, $b.avg_ret_8h, $b.avg_ret_24h)
    }
    $bl = $result.baseline
    Write-Host ("{0,-14} n={1,4}  up8h={2,5}%  up24h={3,5}%  avg8h={4,7}%  avg24h={5,7}%" -f `
        "BASELINE", $bl.n, $bl.up_rate_8h, $bl.up_rate_24h, $bl.avg_ret_8h, $bl.avg_ret_24h) -ForegroundColor Yellow

    $path = Join-Path $OutDir "backtest_funding_$sym.json"
    $result | ConvertTo-Json -Depth 6 | Out-File -FilePath $path -Encoding utf8
    Write-Host "saved: $path`n"
}
