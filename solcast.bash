set -x
#curl "https://api.solcast.com.au/data/forecast/radiation_and_weather?latitude=41.91&longitude=-91.66&hours=48&format=json" -H "Authorization: Bearer 0KqECfn9EkEVGSzy2S_oW1okM2e7PRPi"

curl "https://api.solcast.com.au/rooftop_sites/2e34-57ae-dfae-a555/forecasts?format=json" -H "Authorization: Bearer 0KqECfn9EkEVGSzy2S_oW1okM2e7PRPi"

