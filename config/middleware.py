from django.conf import settings


class StripUrlPrefixMiddleware:
    """Accept the URL prefix when it reaches the app, for direct access without the proxy.

    The proxy strips the prefix before forwarding, but generated links always carry it.
    Opening the app on its own port therefore sends /prefix/... requests; drop the prefix
    so they route like the proxied ones. Does nothing when no prefix is configured.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.prefix = settings.FORCE_SCRIPT_NAME.rstrip("/") if settings.FORCE_SCRIPT_NAME else ""

    def __call__(self, request):
        prefix = self.prefix
        if prefix and (request.path_info == prefix or request.path_info.startswith(prefix + "/")):
            request.path_info = request.path_info[len(prefix):] or "/"
        return self.get_response(request)
