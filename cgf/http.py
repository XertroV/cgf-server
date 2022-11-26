from cgf.consts import SERVER_VERSION
import aiohttp

def get_session():
    return aiohttp.ClientSession(headers={
        'User-Agent': f'CommunityGameFramework/contact=@XertroV/server-version={SERVER_VERSION}/'
    })
