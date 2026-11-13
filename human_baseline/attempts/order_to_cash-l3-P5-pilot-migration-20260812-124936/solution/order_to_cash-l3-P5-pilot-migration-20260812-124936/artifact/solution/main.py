try:
    from .datatypes import ArtifactAgentQuery
    from .detector import respond
except ImportError:
    from datatypes import ArtifactAgentQuery
    from detector import respond


# Execute as python -m solution.main
if __name__ == "__main__":

    for inv in ["INV-3046-DUP",
                "INV-2474",
                "INV-3350-DUP",
                "INV-2994",
                "INV-7984",
                "INV-1696",
                "INV-1077",
                "INV-3704",
                "INV-2241",
                "INV-4706",
                "INV-5433",
                "INV-9644",
                "INV-2585"]:
        q = ArtifactAgentQuery(text=inv)
        result = respond(q)
    