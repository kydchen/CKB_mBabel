"""Regression asserts for split_lang_runs (audit H4: backslash-w matched CJK)."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import detect_direction, should_flush_clause, split_lang_runs

# CJK inside a would-be latin run must not be swallowed
pieces = split_lang_runs('Fiber的feature是把Lightning Network的能力搬到CKB上')
assert len(pieces) == 1 and pieces[0][0] == 'zh', pieces

pieces = split_lang_runs('我们用CKB的Cell model来做state channel的验证')
assert len(pieces) == 1 and pieces[0][0] == 'zh', pieces

# pure English stays one en piece
pieces = split_lang_runs("Let's review the Fiber network upgrade and the Cell model design.")
assert len(pieces) == 1 and pieces[0][0] == 'en', pieces

# zh then long English splits into zh, en
pieces = split_lang_runs('我们这周讨论一下 UTXO。Let\'s review the Fiber network upgrade and the cell model.')
assert [lang for lang, _ in pieces] == ['zh', 'en'], pieces

# short embedded terms never trigger a split
pieces = split_lang_runs('然后呃我们可以，maybe，可能，呃，hi，Matt，可以听下你的想法了。')
assert len(pieces) == 1 and pieces[0][0] == 'zh', pieces

# A CJK name does not flip a predominantly English sentence; English product
# terms likewise do not flip a Chinese sentence.
assert detect_direction('Please ask 张伟 to vote on the proposal.') == ('en', 'zh')
assert detect_direction('我们用 CKB 和 Fiber Network 做测试。') == ('zh', 'en')

# long lines flush only when the current fragment ends at a clause boundary
fragment = '继续说明，'
assert not should_flush_clause('前' * 74 + fragment, fragment)
assert should_flush_clause('前' * 75 + fragment, fragment)
fragment = '仍在同一个词中'
assert not should_flush_clause('前' * 80 + fragment, fragment)

print('split_lang_runs and clause flushing: all asserts pass')
