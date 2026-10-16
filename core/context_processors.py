from .layouts import get_layout


def layout(request):
    """Expose le layout actif aux templates sous ``alfaway_layout``.

    Nom préfixé : ``layout`` est déjà un champ métier (``Feed.layout``).
    """
    return {'alfaway_layout': get_layout()}
