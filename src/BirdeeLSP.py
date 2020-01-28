from pygls.features import *
from pygls.server import LanguageServer
from pygls.types import *
from urllib.parse import unquote
import os
from urllib.parse import urlparse
from pygls.workspace import Document

def uri2ospath(uri: str):
    p = urlparse(uri)
    return os.path.abspath(os.path.join(p.netloc, p.path))

server = LanguageServer()
txt = None

@server.feature(COMPLETION, trigger_characters=[','])
def completions(params: CompletionParams):
    """Returns completion items."""
    return CompletionList(False, [
        CompletionItem('"'),
        CompletionItem('['),
        CompletionItem(']'),
        CompletionItem('{'),
        CompletionItem(123)
    ])

@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def didchange(params: DidChangeTextDocumentParams):
    for ch in params.contentChanges:
        txt.apply_change(ch)

@server.feature(TEXT_DOCUMENT_DID_OPEN)
def didopen(params: DidOpenTextDocumentParams):
    global txt
    txt=Document(params.textDocument.uri)

@server.feature(DOCUMENT_COLOR)
def oncolor(params: DocumentColorParams):
    return [ColorInformation(Range(Position(0,1), Position(0,3)), Color(1,0,0,0))]

@server.feature(COLOR_PRESENTATION)
def colorpresentation(params: ColorPresentationParams):
    return [ColorPresentation("HAHAH")]
    
server.start_io()