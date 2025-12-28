import aiohttp
import asyncio

R6_STATUS_URL = "https://www.ubisoft.com/en-us/game/rainbow-six/siege/status"  # voorbeeld URL, kan aangepast worden

async def fetch_r6_status():
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(R6_STATUS_URL) as resp:
                if resp.status != 200:
                    return {"error": f"Failed to fetch data: {resp.status}"}
                data = await resp.json()
                
                # Voorbeeld structuur aanpassen aan echte API
                servers = {}
                for platform in ["PC", "PS4", "XBOX"]:
                    servers[platform] = {
                        "online": True if data.get(platform, {}).get("status") == "online" else False,
                        "maintenance": True if data.get(platform, {}).get("status") == "maintenance" else False
                    }
                return servers
        except Exception as e:
            return {"error": str(e)}

