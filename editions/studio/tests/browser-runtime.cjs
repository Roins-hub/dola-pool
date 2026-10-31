// Reuse the Python dependency on Windows, macOS, and Linux.
const {execFileSync}=require('node:child_process');
const python=process.env.DOLA_TEST_PYTHON||'python';
const packagePath=execFileSync(python,['-c',
  'from pathlib import Path; import patchright; print(Path(patchright.__file__).parent / "driver" / "package")'
],{encoding:'utf8'}).trim();
module.exports=require(packagePath);
