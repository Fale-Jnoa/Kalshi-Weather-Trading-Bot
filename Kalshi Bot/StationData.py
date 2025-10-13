import requests
from bs4 import BeautifulSoup

site = 'https://www.weather.gov/wrh/timeseries?site=KNYC'
siteData = requests.get(site)
soup = BeautifulSoup(siteData.content, 'html.parser')
print(soup)



# headers = {"User-Agent": "FloodCheck.com"}
# r = requests.get("https://api.weather.gov/stations/KNYC/observations/latest", headers=headers)
# data = r.json()
# highTemp = (data['properties']['maxTemperatureLast24Hours']['value']) 
# tempF = (data['properties']['temperature']['value'] * 9/5) + 32


# if highTemp is not None:
#     print(f"Temperature: {(tempF * 9/5) + 32}°F")
# else:
#     print("High temp data is not available.")

# print(f"Current temp is {tempF}°F") 


