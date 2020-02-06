from pygls.features import *
from pygls.server import LanguageServer
from pygls.types import *
from pygls.uris import from_fs_path, to_fs_path
from urllib.parse import unquote
import os
from pygls.workspace import Document
import birdeec
import math
from threading import Lock

server = LanguageServer()
txt = dict()
root_path = None
source_root_path = None
cache_path = None

def dbgprint(s):
    with open("d:\\birdeelsp.log", "a") as f:
        f.write(str(s)+"\n")

class Compiler:
    def __init__(self):
        self.mutex = Lock()
        self.uri=None
        self.last_status = False
        self.last_compiled_source = None
        self.last_successful_source = dict() # str(uri) -> str(source)
        self.module_metadata = dict() # tuple[module_names] -> str(json)

    def __enter__(self):
        self.mutex.acquire()

    def __exit__(self, ty, value, traceback):
        self.mutex.release()
    
    def switch_to_last_successful(self, uri):
        if uri in self.last_successful_source:
            self.compile(uri, self.last_successful_source[uri])
        else:
            birdeec.clear_compile_unit()

    def on_exit(self):
        for mod in self.module_metadata:
            modname=list(mod)
            modname[-1] = modname[-1] + ".bmm"
            target_path = os.path.join(root_path, cache_path, *modname)
            tdir = os.path.dirname(target_path)
            if not os.path.exists(tdir):
                os.makedirs(tdir)
            with open(target_path, 'w') as f:
                f.write(self.module_metadata[mod])

    '''
    compile a module and its dependencies:
        if it has any uncompiled modules that we can find source in the workspace,
        compile it. Then get and store the dependencies' BMM metadata in memory.
        Then re-compile the module
    '''
    def _docompile(self, fspath, istr):
        def find_module_path(mod, ext):
            modname=list(mod)
            modname[-1] = modname[-1] + ext
            target_path = os.path.join(root_path, source_root_path, *modname)
            if os.path.exists(target_path):
                return target_path
            else:
                return None

        def compileit():
            e = None
            birdeec.set_module_resolver(_module_resolver)
            try:
                birdeec.set_source_file_path(fspath)
                birdeec.clear_compile_unit()
                birdeec.top_level(istr)
                birdeec.process_top_level()
            except birdeec.TokenizerException:
                e=birdeec.get_tokenizer_error()
            except birdeec.CompileException:
                e=birdeec.get_compile_error()
            if not e:
                cur_module = tuple(birdeec.get_module_name().split("."))
                self.module_metadata[cur_module] = birdeec.get_metadata_json()
            return e

        while True:
            e=None
            can_recompile=True
            dependencies=[]
            def _module_resolver(modname, second_chance):
                tmod = tuple(modname)
                nonlocal can_recompile
                if not second_chance:
                    if tmod in self.module_metadata:
                        return ("$InMemoryModule", self.module_metadata[tmod])
                    else:
                        return None
                else:
                    target_path = find_module_path(modname,".bdm")
                    if not target_path:
                        target_path = find_module_path(modname,".txt")
                    if target_path:
                        dependencies.append((tmod, target_path))
                    else:
                        can_recompile=False
                    return None
            e = compileit()
            if not e:
                return e
            if not can_recompile or len(dependencies)==0:
                return e
            # can re-compile
            # first compile dependencies
            for (mod,srcpath) in dependencies:
                if mod in self.module_metadata: 
                    # a dependency may be compiled in another dependency
                    continue
                src = ""
                with open(srcpath) as f:
                    src = f.read()
                sub_e = self._docompile(srcpath, src)
                if sub_e:
                    msg = "While compiling {}, an error occurs: {}".format(srcpath, sub_e.msg)
                    server.show_message(msg, MessageType.Error)
                    return e
        return e

    def compile(self, uri, istr) -> bool:
        if self.uri == uri and istr == self.last_compiled_source:
            return self.last_status

        self.last_compiled_source = istr
        fspath=to_fs_path(uri)
        e = self._docompile(fspath, istr)                
        self.uri= uri
        self.changed=False
        if not e:
            self.last_status=True
            self.last_successful_source[uri] = istr
            server.publish_diagnostics(uri, [])
            return True
        else:
            self.last_status=False
            diag = Diagnostic(Range(
                Position(e.linenumber, e.pos+1), Position(e.linenumber, e.pos+2)
            ), e.msg)
            server.publish_diagnostics(uri, [diag])
            return False

compiler = Compiler()



def find_ast_by_pos(pos: Position):
    tl = birdeec.get_top_level()
    res=[]
    def runfunc(ast: birdeec.StatementAST):
        if not ast:
            return
        if ast.pos.line == pos.line + 1 and ast.pos.pos >= pos.character + 1:
            res.append((ast.pos.pos - pos.character - 1, ast))
        ast.run(runfunc)
    toplevel=[] #list of (line distance, stmt)
    for a in tl:
        toplevel.append((abs(a.pos.line - pos.line -1), a))
    for a in sorted(toplevel, key = lambda x: x[0])[0: 4]: #get nearest 4 toplevel stmt, sorted by line
        runfunc(a[1]) # run into the AST to find the specific statement
    return sorted(res,  key=lambda x: x[0])

def sourcepos2position(pos: birdeec.SourcePos, main_src_uri: str) -> (Position, str):
    if not pos: return None
    uri = from_fs_path(pos.source_path) if pos.source_idx != -1 else main_src_uri
    return (Position(pos.line - 1, pos.pos - 1), uri)

def get_member_def_pos(mem: birdeec.MemberExprAST) -> birdeec.SourcePos:
    if mem.kind == birdeec.MemberExprAST.MemberType.FIELD:
        return mem.field.decl.pos
    elif mem.kind == birdeec.MemberExprAST.MemberType.FUNCTION or mem.kind == birdeec.MemberExprAST.MemberType.VIRTUAL_FUNCTION:
        return mem.func.decl.pos
    elif mem.kind == birdeec.MemberExprAST.MemberType.IMPORTED_DIM:
        return mem.imported_dim.pos
    elif mem.kind == birdeec.MemberExprAST.MemberType.IMPORTED_FUNCTION:
        return mem.imported_func.pos
    return None  

def get_def(uri, istr, pos: Position)-> (Position, str):
    ret=None
    with compiler:
        if not compiler.compile(uri, istr):
            return ret
        asts=find_ast_by_pos(pos)
        for ast in asts:
            impl=ast[1]
            if isinstance(impl, birdeec.LocalVarExprAST):
                ret=sourcepos2position(impl.vardef.pos, uri)
                break
            if isinstance(impl, birdeec.ResolvedFuncExprAST):
                ret=sourcepos2position(impl.funcdef.pos, uri)
                break
            if isinstance(impl, birdeec.MemberExprAST):
                ret=sourcepos2position(get_member_def_pos(impl), uri)
                break	
    return ret

primitive_types=[
                    CompletionItem('byte', kind=CompletionItemKind.Class),
                    CompletionItem('int', kind=CompletionItemKind.Class),
                    CompletionItem('uint', kind=CompletionItemKind.Class),
                    CompletionItem('long', kind=CompletionItemKind.Class),
                    CompletionItem('ulong', kind=CompletionItemKind.Class),
                    CompletionItem('float', kind=CompletionItemKind.Class),
                    CompletionItem('double', kind=CompletionItemKind.Class),
                    CompletionItem('pointer', kind=CompletionItemKind.Class),
                ]

@server.feature(COMPLETION, trigger_characters=[' '])
def completions(params: CompletionParams):
    #bybass a bug (?) of pygls 
    if not hasattr(params.context, 'triggerKind'):
        return None
    if params.context.triggerKind==2:
        if params.context.triggerCharacter==' ':
            t: Document = txt[params.textDocument.uri]
            pos = params.position.character
            istr = t.source.split('\n')[params.position.line]
            if istr[pos-3:pos+1]=="as " or istr[pos-4:pos+1]=="new ":
                    with compiler:
                        compiler.switch_to_last_successful(params.textDocument.uri)
                    class_names = list(birdeec.get_classes(True).keys()) + list(birdeec.get_classes(False).keys())
                    functype_names = list(birdeec.get_functypes(True).keys()) + list(birdeec.get_functypes(False).keys())
                    cls_completion = [CompletionItem(name) for name in class_names]
                    func_completion = [CompletionItem(name, kind=CompletionItemKind.Function) for name in functype_names]
                    return CompletionList(False, primitive_types + cls_completion + func_completion)
    return None

@server.feature(DEFINITION)
def definitions(params: TextDocumentPositionParams):
    uri=params.textDocument.uri
    r, outuri=get_def(uri, txt[uri].source, params.position)
    if r:
        return Location(outuri, Range(
            r, Position(r.line, r.character+1)
        ))
    else:
        return None

@server.feature(INITIALIZE)
def oninitialize(params: InitializeParams):
    global root_path
    root_path = params.rootPath

@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def didchange(params: DidChangeTextDocumentParams):
    for ch in params.contentChanges:
        txt[params.textDocument.uri].apply_change(ch)

@server.feature(TEXT_DOCUMENT_DID_OPEN)
def didopen(params: DidOpenTextDocumentParams):
    uri=params.textDocument.uri
    txt[uri]=Document(uri)
    with compiler:
        compiler.compile(uri, params.textDocument.text)
    
@server.feature(TEXT_DOCUMENT_DID_SAVE)
def didsave(params: DidSaveTextDocumentParams):
    with compiler:
        uri=params.textDocument.uri
        compiler.compile(uri, txt[uri].source)

@server.feature(WORKSPACE_DID_CHANGE_CONFIGURATION)
def onconfigchange(params: DidChangeConfigurationParams):
    global source_root_path, cache_path
    source_root_path = params.settings.birdeeLanguageServer.sourceRoot
    cache_path = params.settings.birdeeLanguageServer.lspCache

@server.feature(SHUTDOWN)
def onexit(params):
    compiler.on_exit()

server.start_io()